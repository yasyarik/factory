import sqlite3, secrets, re, json, urllib.request, urllib.error, socket, time, os, signal
from datetime import datetime, timezone

DB='/var/www/content-factory-yaswine/factory.sqlite'
BASE='http://127.0.0.1:3199'
LOG='/var/www/content-factory-yaswine/country_batch.log'

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

def post_json(url, payload=None, timeout=240):
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

log('BATCH_RESILIENT_START countries=' + ', '.join(countries))

for key in countries:
    ent = cur.execute("SELECT id, entity_key, slug, title FROM seo_entities WHERE entity_type='country' AND lower(entity_key)=lower(?) LIMIT 1", (key,)).fetchone()
    if not ent:
      log(f"{key}: missing entity")
      continue

    row = cur.execute("""
      SELECT sj.id as job_id, sj.status as seo_status, j.status as job_status, j.published_url
      FROM seo_jobs sj JOIN jobs j ON j.id=sj.id
      WHERE sj.entity_id=? AND sj.entity_type='country'
      ORDER BY sj.updated_at DESC LIMIT 1
    """, (ent['id'],)).fetchone()

    if row and (row['job_status']=='PUBLISHED' or row['seo_status']=='PUBLISHED'):
      log(f"{key}: already published -> {row['published_url']}")
      continue

    if row:
      job_id = row['job_id']
    else:
      job_id = secrets.token_hex(12)
      t = (ent['title'] or f"Wine in {ent['entity_key'].title()}").strip()
      slug = (ent['slug'] or '').strip() or f"wine-country-{slugify(ent['entity_key'])}"
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

    st, gen = post_json(f"{BASE}/api/seo/jobs/{job_id}/generate", {})
    okg = bool(gen.get('ok')) if isinstance(gen, dict) else False
    if not okg:
      log(f"{key}: generate failed http={st} err={gen}")
      continue

    st2, pub = post_json(f"{BASE}/api/seo/jobs/{job_id}/publish", {})
    okp = bool(pub.get('ok')) if isinstance(pub, dict) else False
    if okp:
      log(f"{key}: published -> {pub.get('publishedUrl')}")
    else:
      log(f"{key}: publish failed http={st2} err={pub}")

log('BATCH_RESILIENT_DONE')
