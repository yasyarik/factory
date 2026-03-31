import sqlite3, secrets, re, json, urllib.request, urllib.error, time
from datetime import datetime, timezone

DB='/var/www/content-factory/factory.sqlite'
BASE='http://127.0.0.1:3199'
LOG='/var/www/content-factory/country_batch.log'

countries = [
    'france','italy','spain','united states','argentina','chile','australia','portugal','germany','south africa',
    'new zealand','austria','greece','brazil','hungary',
    'uruguay','mexico','romania','switzerland','georgia'
]

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def log(msg):
    line=f"[{now()}] {msg}\n"
    print(line, end='')
    with open(LOG,'a',encoding='utf-8') as f:
        f.write(line)

def slugify(s:str)->str:
    s=s.strip().lower()
    s=re.sub(r'[^a-z0-9\s-]','',s)
    s=re.sub(r'\s+','-',s)
    s=re.sub(r'-+','-',s)
    return s.strip('-') or 'country'

def post_json(url, payload=None, timeout=3600):
    data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type','application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8','replace')
        try: payload = json.loads(raw)
        except Exception: payload = {'error': raw}
        return e.code, payload
    except Exception as e:
        return 0, {'error': str(e)}

con=sqlite3.connect(DB)
con.row_factory=sqlite3.Row
cur=con.cursor()

log('BATCH_START countries=' + ', '.join(countries))

summary=[]
for key in countries:
    ent = cur.execute("SELECT id, entity_key, slug, title FROM seo_entities WHERE entity_type='country' AND lower(entity_key)=lower(?) LIMIT 1", (key,)).fetchone()
    if not ent:
      log(f"{key}: missing entity")
      summary.append({'country':key,'status':'MISSING_ENTITY'})
      continue

    row = cur.execute("""
      SELECT sj.id as job_id, sj.status as seo_status, j.status as job_status, j.published_url
      FROM seo_jobs sj JOIN jobs j ON j.id=sj.id
      WHERE sj.entity_id=? AND sj.entity_type='country'
      ORDER BY sj.updated_at DESC LIMIT 1
    """, (ent['id'],)).fetchone()

    if row and (row['job_status']=='PUBLISHED' or row['seo_status']=='PUBLISHED'):
      log(f"{key}: already published -> {row['published_url']}")
      summary.append({'country':key,'status':'ALREADY_PUBLISHED','url':row['published_url']})
      continue

    if row:
      job_id = row['job_id']
    else:
      job_id = secrets.token_hex(12)
      t = (ent['title'] or f"Wine in {ent['entity_key'].title()}").strip()
      slug = (ent['slug'] or '').strip() or f"topic-cluster-{slugify(ent['entity_key'])}"
      ts = now()
      cur.execute("""
        INSERT INTO jobs (id, topic, slug, status, category, visibility, product_mode, engagement_mode, lead_magnet_mode, created_at, updated_at)
        VALUES (?, ?, ?, 'NEW', 'Wine Regions', 'public', 0, 0, 0, ?, ?)
      """, (job_id, t, slug, ts, ts))
      con.commit()

    ts = now()
    cur.execute("""
      INSERT OR REPLACE INTO seo_jobs (id, entity_id, entity_type, status, error, output_path, created_at, updated_at)
      VALUES (?, ?, 'country', 'QUEUED', NULL,
              COALESCE((SELECT output_path FROM seo_jobs WHERE id=?), NULL),
              COALESCE((SELECT created_at FROM seo_jobs WHERE id=?), ?),
              ?)
    """, (job_id, ent['id'], job_id, job_id, ts, ts))
    con.commit()

    okg=False
    for attempt in (1,2):
      st, gen = post_json(f"{BASE}/api/seo/jobs/{job_id}/generate", {})
      okg = bool(gen.get('ok')) if isinstance(gen, dict) else False
      if okg:
        break
      log(f"{key}: generate attempt {attempt} failed http={st} err={gen}")
      time.sleep(2)

    if not okg:
      summary.append({'country':key,'job_id':job_id,'status':'GENERATE_FAILED'})
      continue

    okp=False
    last=None
    for attempt in (1,2):
      st2, pub = post_json(f"{BASE}/api/seo/jobs/{job_id}/publish", {})
      okp = bool(pub.get('ok')) if isinstance(pub, dict) else False
      last=(st2,pub)
      if okp:
        break
      log(f"{key}: publish attempt {attempt} failed http={st2} err={pub}")
      time.sleep(2)

    if okp:
      url = (last[1] or {}).get('publishedUrl')
      log(f"{key}: published -> {url}")
      summary.append({'country':key,'job_id':job_id,'status':'PUBLISHED','url':url})
    else:
      summary.append({'country':key,'job_id':job_id,'status':'PUBLISH_FAILED','detail':str(last)})

log('BATCH_DONE ' + json.dumps(summary, ensure_ascii=False))
