#!/usr/bin/env python3
"""Avrupa oranları toplayıcı (OddsPapi).
Modlar: kesif  -> şirket, market, lig ve takım listelerini indirir
        sabah  -> 1 ücretli istekle maç listesi + ücretsiz geçmiş-oran uç noktasıyla tüm şirketler
        aksam  -> aynısı, sadece akşam şirketleri
Sadece Python standart kütüphanesi kullanır."""
import csv, datetime as dt, json, os, sys, time, urllib.error, urllib.parse, urllib.request

BASE = "https://api.oddspapi.io/v4"
KEY = os.environ.get("ODDSPAPI_KEY", "").strip()
ROOT = "avrupa"
SABIT = os.path.join(ROOT, "sabit")
TR = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime.now(dt.timezone.utc)
LOG = []
USED = 0


def log(s):
    print(s, flush=True)
    LOG.append(s)


class ApiError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code


def get(path, params=None, billable=True, pause=1.3, _retry=True):
    global USED
    p = dict(params or {})
    p["apiKey"] = KEY
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(p, safe=",")
    req = urllib.request.Request(url, headers={"User-Agent": "oran-takip/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        if billable:
            USED += 1
        if e.code == 429 and _retry and "REQUEST_LIMIT" not in body:
            time.sleep(12)
            return get(path, params, billable, pause, False)
        raise ApiError(e.code, body)
    finally:
        time.sleep(pause)
    if billable:
        USED += 1
    return data


def find_key(obj, name):
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        for v in obj.values():
            r = find_key(v, name)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, name)
            if r is not None:
                return r
    return None


def quota():
    try:
        a = get("account", billable=False)
        lim, cnt = find_key(a, "request_limit"), find_key(a, "request_count")
        if lim is not None and cnt is not None:
            return int(lim), int(cnt)
    except Exception as e:
        log(f"Kota okunamadı: {e}")
    return None, None


def can_spend(cfg):
    if USED >= cfg["maks_istek_calisma"]:
        log(f"Bu çalışmanın istek sınırına ({cfg['maks_istek_calisma']}) ulaşıldı.")
        return False
    lim, cnt = quota()
    if lim is not None and lim - cnt <= cfg["rezerv_istek"]:
        log(f"Aylık kota korumaya girdi: {cnt}/{lim} kullanıldı, {cfg['rezerv_istek']} istek yedekte tutuluyor.")
        return False
    return True


def rjson(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def wjson(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def as_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "items", "results"):
            if isinstance(d.get(k), list):
                return d[k]
        return list(d.values()) if all(isinstance(v, dict) for v in d.values()) else [d]
    return []


def age_days(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return 1e9


# ---------------- sabit listeler ----------------
def refresh(name, path, params, cfg):
    if not can_spend(cfg):
        return False
    try:
        wjson(os.path.join(SABIT, name + ".json"), get(path, params))
        log(f"{name} listesi güncellendi.")
        return True
    except ApiError as e:
        log(f"{name} listesi alınamadı: {e}")
        return False


def kesif(cfg):
    refresh("bookmakers", "bookmakers", {}, cfg)
    refresh("markets", "markets", {"language": "en"}, cfg)
    refresh("tournaments", "tournaments", {"sportId": 10}, cfg)
    refresh("participants", "participants", {"sportId": 10}, cfg)


def bookmaker_index():
    idx = []
    for b in as_list(rjson(os.path.join(SABIT, "bookmakers.json"), [])):
        if not isinstance(b, dict):
            continue
        slug = b.get("slug") or b.get("bookmakerSlug") or b.get("bookmaker")
        name = b.get("bookmakerName") or b.get("name") or slug
        if slug:
            idx.append((str(slug), str(name)))
    return idx


def resolve(wanted, idx):
    if not idx:
        return wanted
    w = wanted.lower()
    for s, n in idx:
        if s.lower() == w:
            return s
    for s, n in idx:
        if n.lower() == w:
            return s
    c = [s for s, n in idx if w in s.lower() or w in n.lower().replace(" ", "")]
    return sorted(c, key=len)[0] if c else None


def participant_names():
    d = rjson(os.path.join(SABIT, "participants.json"), [])
    out = {}
    if isinstance(d, dict) and d and all(not isinstance(v, (dict, list)) for v in d.values()):
        return {str(k): str(v) for k, v in d.items()}
    for p in as_list(d):
        if isinstance(p, dict):
            pid = p.get("participantId", p.get("id"))
            nm = p.get("participantName") or p.get("name") or p.get("shortName")
            if pid is not None and nm:
                out[str(pid)] = str(nm)
    return out


def market_index():
    idx = {}
    for m in as_list(rjson(os.path.join(SABIT, "markets.json"), [])):
        if isinstance(m, dict) and "marketId" in m:
            idx[str(m["marketId"])] = m
    return idx


BAD = ("corner", "card", "booking", "player", "minute", "penalt", "exact", "correct score", "team total", "odd/even", "odd even")


def keep_market(m):
    if not m or m.get("playerProp"):
        return False
    n = str(m.get("marketName", "")).lower()
    if any(b in n for b in BAD):
        return False
    h = m.get("handicap") or 0
    if m.get("marketType") == "1x2" and "handicap" not in n:
        return True
    if "both teams" in n or "double chance" in n:
        return True
    if "over under" in n or "total" in n:
        return float(h) in (0.5, 1.5, 2.5, 3.5, 4.5)
    if "handicap" in n:
        return abs(float(h)) <= 2
    return False


# ---------------- oran toplama ----------------
def select_tournaments(cfg):
    ts = [t for t in as_list(rjson(os.path.join(SABIT, "tournaments.json"), [])) if isinstance(t, dict)]
    cats = sorted({str(t.get("categorySlug")) for t in ts})
    want = [c.lower() for c in cfg["ulkeler"]]
    per = cfg.get("ulke_basina_lig", 3)
    extra = cfg.get("ulke_ozel_lig_sayisi", {})
    sel = {c: [] for c in want}
    for t in ts:  # API listesi önem sırasına göre geliyor
        c = str(t.get("categorySlug", "")).lower()
        n = (t.get("tournamentName") or "").lower()
        if c not in sel or any(x in n for x in cfg["haric"]):
            continue
        if (t.get("futureFixtures") or 0) + (t.get("upcomingFixtures") or 0) <= 0:
            continue
        if len(sel[c]) < extra.get(c, per):
            sel[c].append(t)
    out = [t for c in want for t in sel[c]]
    return out[: cfg["maks_turnuva"]], cats


def list_fixtures(cfg, tours):
    """Tek (ücretli) istekle önümüzdeki saatlerin tüm futbol maçlarını alır, seçili liglere süzer."""
    frm = NOW - dt.timedelta(minutes=10)
    to = NOW + dt.timedelta(hours=min(cfg["pencere_saat"], 47))
    if not can_spend(cfg):
        return []
    try:
        data = get("fixtures", {"sportId": 10, "from": frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"), "statusId": 0, "hasOdds": "true"}, pause=2.2)
    except ApiError as e:
        log(f"Maç listesi alınamadı: {e}")
        return []
    ids = {str(t["tournamentId"]) for t in tours}
    fx = [f for f in as_list(data) if isinstance(f, dict) and str(f.get("tournamentId")) in ids]
    fx.sort(key=lambda f: f.get("startTime") or "")
    log(f"API'de {len(as_list(data))} maç var, seçili liglerde {len(fx)} maç.")
    return fx[: cfg["maks_mac"]]


def latest_and_open(history):
    rows = [h for h in history if isinstance(h, dict) and h.get("price")]
    if not rows:
        return None
    rows.sort(key=lambda h: h.get("createdAt") or "")
    act = [h for h in rows if h.get("active", True)]
    last = (act or rows)[-1]
    return {"price": last["price"], "changedAt": last.get("createdAt", ""), "open": rows[0]["price"], "active": True}


def fetch_history(fixtures, sirketler, cfg, idx):
    """Ücretsiz geçmiş-oran uç noktasıyla her maçın oranlarını şirket şirket doldurur."""
    slugs = []
    for w in sirketler:
        s_ = resolve(w, idx)
        if s_:
            slugs.append(s_)
        else:
            log(f"Şirket bulunamadı: {w}")
    per_call = cfg.get("istek_basina_sirket", 3)
    deadline = time.time() + cfg["maks_dakika"] * 60
    out = {}
    for f in fixtures:
        out[f["fixtureId"]] = dict(f, bookmakerOdds={})
    groups = [slugs[i:i + per_call] for i in range(0, len(slugs), per_call)]
    gi = 0
    while gi < len(groups):  # önce tüm maçlar için 1. grup (Pinnacle), sonra diğerleri
        g = groups[gi]
        got, fail = 0, 0
        for fid in list(out):
            if time.time() > deadline:
                log(f"Süre sınırı ({cfg['maks_dakika']} dk) doldu, kalan şirketler atlandı.")
                return out
            try:
                data = get("historical-odds", {"fixtureId": fid, "bookmakers": ",".join(g)}, billable=False, pause=5.3)
            except ApiError as e:
                if e.code == 400 and "bookmaker" in str(e).lower() and len(g) > 1 and per_call > 1:
                    log("API tek istekte tek şirket istiyor; tek tek çekmeye geçiliyor.")
                    per_call = 1
                    rest = [x for gg in groups[gi:] for x in gg]
                    groups = groups[:gi] + [[x] for x in rest]
                    gi -= 1
                    break
                fail += 1
                if fail <= 3:
                    log(f"{fid} ({','.join(g)}) alınamadı: {e}")
                if e.code == 429 and "REQUEST_LIMIT" in str(e):
                    return out
                continue
            bks = (data or {}).get("bookmakers") or {}
            for bk, bd in bks.items():
                mk = {}
                for mid, md in ((bd or {}).get("markets") or {}).items():
                    outs = {}
                    for oid, od in ((md or {}).get("outcomes") or {}).items():
                        hist = ((od or {}).get("players") or {}).get("0")
                        lo = latest_and_open(hist if isinstance(hist, list) else [hist] if hist else [])
                        if lo:
                            outs[oid] = {"players": {"0": lo}}
                    if outs:
                        mk[mid] = {"outcomes": outs}
                if mk:
                    out[fid]["bookmakerOdds"][bk] = {"markets": mk}
                    got += 1
        else:
            log(f"{', '.join(g)}: {len(out)} maçta {got} şirket-maç oranı geldi.")
        gi += 1
    return out


def parse_time(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def devig(prices):
    inv = [1 / p for p in prices]
    s = sum(inv)
    return [x / s for x in inv], s - 1


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "sabah").lower()
    if not KEY:
        sys.exit("ODDSPAPI_KEY gizli anahtarı tanımlı değil. Depo > Settings > Secrets and variables > Actions bölümüne ekle.")
    cfg = rjson(os.path.join(ROOT, "ayarlar.json"))
    if not cfg:
        sys.exit("avrupa/ayarlar.json bulunamadı.")
    lim, cnt = quota()
    log(f"Mod: {mode}. Kota: {cnt}/{lim}" if lim else f"Mod: {mode}.")

    if mode == "kesif" or not os.path.exists(os.path.join(SABIT, "markets.json")):
        kesif(cfg)
    if mode == "kesif":
        finish(mode, None, None, None)
        return
    if age_days(os.path.join(SABIT, "tournaments.json")) > 7:
        refresh("tournaments", "tournaments", {"sportId": 10}, cfg)

    tours, cats = select_tournaments(cfg)
    log(f"Seçilen lig sayısı: {len(tours)}")
    if not tours:
        log("Ayarlardaki ülkelerle eşleşen lig yok. Kullanılabilir ülke kodları: " + ", ".join(cats[:200]))
    idx = bookmaker_index()
    sirketler = cfg["sirketler"] if mode == "sabah" else cfg["aksam_sirketler"]
    log("Şirketler: " + ", ".join(sirketler))
    fixtures = list_fixtures(cfg, tours)
    fx = fetch_history(fixtures, sirketler, cfg, idx) if fixtures else {}
    names = participant_names()
    finish(mode, fx, tours, names, cfg)


def finish(mode, fx, tours, names, cfg=None):
    stamp = NOW.astimezone(TR).strftime("%Y-%m-%d_%H%M")
    rows, cons = [], []
    if fx:
        mk = market_index()
        tmap = {str(t["tournamentId"]): t for t in tours}
        lo, hi = NOW - dt.timedelta(hours=2), NOW + dt.timedelta(hours=cfg["pencere_saat"])
        for fid, f in fx.items():
            st = parse_time(f.get("startTime"))
            if not st or not (lo <= st <= hi):
                continue
            t = tmap.get(str(f.get("tournamentId")), {})
            local = st.astimezone(TR)
            home = f.get("participant1Name") or names.get(str(f.get("participant1Id")), str(f.get("participant1Id")))
            away = f.get("participant2Name") or names.get(str(f.get("participant2Id")), str(f.get("participant2Id")))
            base = [local.strftime("%d.%m.%Y"), local.strftime("%H:%M"), f.get("categoryName") or t.get("categoryName", ""),
                    f.get("tournamentName") or t.get("tournamentName", ""), home, away, fid]
            per = {}  # (market, bookmaker) -> {outcome: price}
            for bk, bo in (f.get("bookmakerOdds") or {}).items():
                if not isinstance(bo, dict) or bo.get("suspended"):
                    continue
                for mid, md in (bo.get("markets") or {}).items():
                    m = mk.get(str(mid))
                    if not keep_market(m):
                        continue
                    onames = {str(o["outcomeId"]): o.get("outcomeName") for o in m.get("outcomes", [])}
                    for oid, od in (md.get("outcomes") or {}).items():
                        pl = (od.get("players") or {}).get("0") or {}
                        price = pl.get("price")
                        if not price or not pl.get("active", True) or price <= 1:
                            continue
                        rows.append(base + [bk, m.get("marketName"), m.get("handicap"), onames.get(str(oid), oid), pl.get("open", ""), price, pl.get("changedAt", "")])
                        per.setdefault((str(mid), bk), {})[onames.get(str(oid), str(oid))] = price
            # uzlaşı: 1X2, 2.5 Alt/Üst, KG
            c = {"tarih": base[0], "saat": base[1], "ulke": base[2], "lig": base[3], "ev": home, "deplasman": away, "fixtureId": fid}
            for mid, m in mk.items():
                n = str(m.get("marketName", "")).lower()
                key = None
                if m.get("marketType") == "1x2" and m.get("period") == "fulltime" and "handicap" not in n and not any(b in n for b in BAD):
                    key = "ms"
                elif ("over under" in n) and m.get("period") == "fulltime" and float(m.get("handicap") or 0) == 2.5 and not any(b in n for b in BAD):
                    key = "au25"
                elif "both teams to score" in n and m.get("period") == "fulltime" and not any(b in n for b in BAD):
                    key = "kg"
                if not key:
                    continue
                outs = [o.get("outcomeName") for o in m.get("outcomes", [])]
                probs, sharp, best = [], None, {}
                for (mm, bk), pr in per.items():
                    if mm != mid or not all(o in pr for o in outs):
                        continue
                    p, _ = devig([pr[o] for o in outs])
                    probs.append(p)
                    if bk.startswith("pinnacle"):
                        sharp = p
                    for o in outs:
                        if pr[o] > best.get(o, (0, ""))[0]:
                            best[o] = (pr[o], bk)
                if not probs:
                    continue
                avg = [sum(x[i] for x in probs) / len(probs) for i in range(len(outs))]
                c[key] = {"sirket_sayisi": len(probs),
                          "uzlasi_adil_oran": {o: round(1 / a, 3) for o, a in zip(outs, avg)},
                          "pinnacle_adil_oran": {o: round(1 / a, 3) for o, a in zip(outs, sharp)} if sharp else None,
                          "en_yuksek_oran": {o: {"oran": best[o][0], "sirket": best[o][1]} for o in outs if o in best}}
            if len(c) > 7:
                cons.append(c)
        rows.sort(key=lambda r: (r[0], r[1], r[4]))
        if rows:
            os.makedirs(os.path.join(ROOT, "gunluk"), exist_ok=True)
            with open(os.path.join(ROOT, "gunluk", stamp + ".csv"), "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(["tarih", "saat", "ulke", "lig", "ev", "deplasman", "fixtureId", "sirket", "market", "cizgi", "secim", "acilis_oran", "son_oran", "degisim_utc"])
                w.writerows(rows)
        if cons:
            cons.sort(key=lambda c: (c["tarih"], c["saat"]))
            wjson(os.path.join(ROOT, "uzlasi", stamp + ".json"), cons)
            wjson(os.path.join(ROOT, "uzlasi", "son.json"), {"olusturma": stamp, "maclar": cons})
        log(f"Kaydedildi: {len({r[6] for r in rows})} maç, {len(rows)} oran satırı, {len(cons)} maç için uzlaşı fiyatı.")
    lim, cnt = quota()
    log(f"Bu çalışmada {USED} istek harcandı." + (f" Aylık kullanım: {cnt}/{lim}." if lim else ""))
    idx = bookmaker_index()
    if cfg is None:
        cfg = rjson(os.path.join(ROOT, "ayarlar.json"), {})
    lines = [f"# Son çalışma: {stamp} ({mode})", ""] + [f"- {s}" for s in LOG]
    if idx and cfg:
        lines += ["", "## Ayarlardaki şirketler", ""]
        for w in cfg.get("sirketler", []):
            lines.append(f"- {w} → {resolve(w, idx) or 'BULUNAMADI'}")
    with open(os.path.join(ROOT, "durum.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(ROOT, "gecmis.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {mode} istek={USED} " + (f"kota={cnt}/{lim}" if lim else "") + "\n")


if __name__ == "__main__":
    main()
