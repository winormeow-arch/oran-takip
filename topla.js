// Nesine bülten oranlarını toplar, gün içi hareketleri hesaplar.
// GitHub Actions içinde 10 dakikada bir çalışır. Bağımlılık yok, Node 20+.

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { execSync } from 'node:child_process';

const BULTEN = 'https://bulten.nesine.com/api/bulten/getprebultenfull';

const DURUM_YOL   = 'data/durum.json';
const HAREKET_YOL = 'data/hareket.json';
const GECMIS_DIZIN = 'data/gecmis';
const KAPANIS_DIZIN = 'data/kapanis';

// Bir hareketin listeye girmesi için gereken en küçük değişim (%)
const ESIK = 2;
// Listede en fazla kaç satır tutulsun
const LIMIT = 800;

// Tek çalışmada kaç örnek alınsın ve aralarında kaç saniye beklensin.
// GitHub'ın cron'u gecikebildiği için örnekleme işin içinde yapılıyor.
const TUR = Number(process.env.TUR || 11);
const ARA = Number(process.env.ARA || 300);

// ------------------------------------------------------------------
// İzlenen marketler
// ------------------------------------------------------------------

const GOL_AU = [11, 12, 13, 155, 207];
const IY_AU  = [14, 15, 209];

function marketAdi(m) {
    const sov = Number(m.SOV);
    switch (m.MTID) {
        case 1:  return { ad: 'MS',    sec: ['1', 'X', '2'] };
        case 3:  return { ad: 'ÇŞ',    sec: ['1-X', '1-2', 'X-2'] };
        case 7:  return { ad: 'İY MS', sec: ['1', 'X', '2'] };
        case 38: return { ad: 'KG',    sec: ['Var', 'Yok'] };
        case 216: return [8.5, 9.5, 10.5].includes(sov)
            ? { ad: `Korner ${sov}`, sec: ['Alt', 'Üst'] } : null;
    }
    if (GOL_AU.includes(m.MTID) && [1.5, 2.5, 3.5].includes(sov)) {
        return { ad: `${sov} Gol`, sec: ['Alt', 'Üst'] };
    }
    if (IY_AU.includes(m.MTID) && [0.5, 1.5].includes(sov)) {
        return { ad: `İY ${sov} Gol`, sec: ['Alt', 'Üst'] };
    }
    return null;
}

// ------------------------------------------------------------------
// Yardımcılar
// ------------------------------------------------------------------

const bugun = () => new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Istanbul', year: 'numeric', month: '2-digit', day: '2-digit'
}).format(new Date());

async function jsonOku(yol, varsayilan) {
    if (!existsSync(yol)) return varsayilan;
    try { return JSON.parse(await readFile(yol, 'utf8')); } catch { return varsayilan; }
}

async function jsonYaz(yol, veri) {
    await mkdir(yol.slice(0, yol.lastIndexOf('/')), { recursive: true });
    await writeFile(yol, JSON.stringify(veri), 'utf8');
}

async function bulteniCek() {
    // Test için: NESINE_DOSYA=ornek.json node topla.js
    if (process.env.NESINE_DOSYA) {
        return JSON.parse(await readFile(process.env.NESINE_DOSYA, 'utf8'));
    }
    const r = await fetch(BULTEN, {
        headers: {
            'accept': 'application/json',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'
        }
    });
    if (!r.ok) throw new Error(`Bülten alınamadı: HTTP ${r.status}`);
    return r.json();
}

// Yanıtın neresinde olursa olsun maç dizisini bul
function olaylariBul(x, d = 0, out = []) {
    if (!x || typeof x !== 'object' || d > 6 || out.length > 8000) return out;
    if (Array.isArray(x)) { for (const y of x) olaylariBul(y, d + 1, out); return out; }
    if (typeof x.HN === 'string' && typeof x.AN === 'string' && Array.isArray(x.MA)) {
        out.push(x); return out;
    }
    for (const v of Object.values(x)) olaylariBul(v, d + 1, out);
    return out;
}

// ------------------------------------------------------------------
// Ana akış
// ------------------------------------------------------------------

async function birTur() {
    const simdi = new Date();
    const gun = bugun();

    const json = await bulteniCek();
    const olaylar = olaylariBul(json).filter(o => o.GT === 1);   // futbol
    if (!olaylar.length) throw new Error('Bültende futbol maçı bulunamadı');

    let durum = await jsonOku(DURUM_YOL, { gun, maclar: {} });

    // Gün döndüyse önceki günü arşivle ve sıfırla
    if (durum.gun !== gun) {
        if (Object.keys(durum.maclar || {}).length) {
            await jsonYaz(`${GECMIS_DIZIN}/${durum.gun}.json`, durum);
        }
        durum = { gun, maclar: {} };
    }

    let yeniMac = 0, yeniOran = 0, degisen = 0;

    for (const o of olaylar) {
        const kod = String(o.C ?? o.NID ?? `${o.HN}-${o.AN}-${o.D}-${o.T}`);

        let mac = durum.maclar[kod];
        if (!mac) {
            mac = durum.maclar[kod] = {
                ev: o.HN, dep: o.AN, tarih: o.D, saat: o.T,
                baslangic: o.ESD || 0, oranlar: {}
            };
            yeniMac++;
        }
        mac.baslangic = o.ESD || mac.baslangic;

        for (const m of o.MA || []) {
            const bilgi = marketAdi(m);
            if (!bilgi) continue;

            for (const s of m.OCA || []) {
                const oran = Number(s.O);
                if (!(oran > 1.01)) continue;                 // 1.00 = kapalı seçenek
                const secAd = bilgi.sec[s.N - 1];
                if (!secAd) continue;

                const anahtar = `${bilgi.ad}|${secAd}`;
                const kayit = mac.oranlar[anahtar];

                if (!kayit) {
                    // i: günün ilk oranı, s: son, a: dip, u: tepe, n: değişim sayısı
                    mac.oranlar[anahtar] = { i: oran, s: oran, a: oran, u: oran, n: 0 };
                    yeniOran++;
                } else if (Math.abs(kayit.s - oran) > 0.001) {
                    kayit.s = oran;
                    kayit.n++;
                    if (oran < kayit.a) kayit.a = oran;
                    if (oran > kayit.u) kayit.u = oran;
                    degisen++;
                }
            }
        }
    }

    // Başlayan maçın son oranını kapanış olarak dondur, sonra listeden düş
    const simdiMs = simdi.getTime();
    const kapanisYol = `${KAPANIS_DIZIN}/${gun}.json`;
    const kapanis = await jsonOku(kapanisYol, {});
    let donan = 0;

    for (const [kod, mac] of Object.entries(durum.maclar)) {
        if (!mac.baslangic || mac.baslangic > simdiMs) continue;
        if (!kapanis[kod]) {
            kapanis[kod] = {
                ev: mac.ev, dep: mac.dep, tarih: mac.tarih, saat: mac.saat,
                baslangic: mac.baslangic, donduruldu: simdi.toISOString(),
                oranlar: Object.fromEntries(Object.entries(mac.oranlar).map(
                    ([a, k]) => [a, { ilk: k.i, kapanis: k.s, dip: k.a, tepe: k.u, adet: k.n }]
                ))
            };
            donan++;
        }
        if (mac.baslangic < simdiMs - 3 * 3600 * 1000) delete durum.maclar[kod];
    }

    if (donan) await jsonYaz(kapanisYol, kapanis);

    durum.guncelleme = simdi.toISOString();
    await jsonYaz(DURUM_YOL, durum);

    // ------------------------------------------------------------------
    // Hareket listesi
    // ------------------------------------------------------------------

    const hareketler = [];

    for (const [kod, mac] of Object.entries(durum.maclar)) {
        for (const [anahtar, k] of Object.entries(mac.oranlar)) {
            if (!k.n) continue;
            const yuzde = ((k.s - k.i) / k.i) * 100;
            if (Math.abs(yuzde) < ESIK) continue;
            const [market, secim] = anahtar.split('|');
            hareketler.push({
                kod, ev: mac.ev, dep: mac.dep, tarih: mac.tarih, saat: mac.saat,
                market, secim,
                ilk: +k.i.toFixed(2), son: +k.s.toFixed(2),
                dip: +k.a.toFixed(2), tepe: +k.u.toFixed(2),
                yuzde: +yuzde.toFixed(1), adet: k.n
            });
        }
    }

    hareketler.sort((a, b) => Math.abs(b.yuzde) - Math.abs(a.yuzde));

    await jsonYaz(HAREKET_YOL, {
        guncelleme: simdi.toISOString(),
        gun,
        macSayisi: Object.keys(durum.maclar).length,
        toplamHareket: hareketler.length,
        hareketler: hareketler.slice(0, LIMIT)
    });

    console.log(
        `${gun} · ${olaylar.length} maç okundu · takipte ${Object.keys(durum.maclar).length} maç · ` +
        `yeni maç ${yeniMac} · yeni oran ${yeniOran} · değişen ${degisen} · listede ${hareketler.length}`
    );
}

function depoyaYaz() {
    if (process.env.GIT_YAZ !== '1') return;
    try {
        execSync('git add data', { stdio: 'inherit' });
        const fark = execSync('git diff --staged --name-only').toString().trim();
        if (!fark) { console.log('  değişiklik yok, commit atlandı'); return; }
        const zaman = new Intl.DateTimeFormat('tr-TR', {
            timeZone: 'Europe/Istanbul', dateStyle: 'short', timeStyle: 'short'
        }).format(new Date());
        execSync(`git commit -q -m "oran: ${zaman}"`, { stdio: 'inherit' });

        // Baska bir calisma ayni dosyalara yazmis olabilir.
        // Cakismada bizim surumumuz kazansin; rebase patlarsa yarim birakma.
        try {
            execSync('git pull --rebase -X theirs --autostash -q', { stdio: 'inherit' });
        } catch {
            try { execSync('git rebase --abort', { stdio: 'ignore' }); } catch {}
            console.log('  cakisma: rebase iptal edildi, sonraki turda tekrar denenecek');
            return;
        }

        try {
            execSync('git push -q', { stdio: 'inherit' });
        } catch {
            console.log('  push reddedildi, sonraki turda tekrar denenecek');
            return;
        }
        console.log('  depoya yazıldı');
    } catch (e) {
        console.log('  depoya yazılamadı: ' + e.message);
    }
}

const uyu = ms => new Promise(r => setTimeout(r, ms));

for (let t = 1; t <= TUR; t++) {
    const saat = new Intl.DateTimeFormat('tr-TR', {
        timeZone: 'Europe/Istanbul', timeStyle: 'medium'
    }).format(new Date());
    process.stdout.write(`[${t}/${TUR}] ${saat} · `);
    try {
        await birTur();
        depoyaYaz();
    } catch (e) {
        console.log('  tur hatası: ' + e.message);
    }
    if (t < TUR) await uyu(ARA * 1000);
}
