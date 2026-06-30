#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ladik (Samsun) Akilli Haber Botu
--------------------------------
Google News + Bing News'i "Ladik" icin tarar, SADECE SON GUNLERDEKI haberleri
alir, tekrarlari eler, Gemini ile suzup ozetler, Telegram'a gonderir.
GitHub Actions tarafindan zamanli calistirilir.
"""

import os
import re
import sys
import json
import html
import time
import calendar
import difflib
import hashlib

import requests
import feedparser

# ---- Gizli bilgiler (GitHub Secrets'tan gelir) ----
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ---- Ayarlar ----
GEMINI_MODEL = "gemini-2.5-flash"

RSS_FEEDS = [
    # Genel haber indeksleri (binlerce haber sitesini tarar)
    "https://news.google.com/rss/search?q=Ladik&hl=tr&gl=TR&ceid=TR:tr",
    "https://www.bing.com/news/search?q=Ladik&format=rss",
    # Samsun yerel kaynak (tum Samsun gelir; Gemini Ladik'e daraltir) - dogrulandi
    "https://www.samsunhaberajansi.com/rss.xml",
    # internetsamsun: DOGRUDAN Ladik'e ozel besleme (sadece Ladik haberi)
    "https://www.internetsamsun.com/samsun/ladik/sondakika.rss",
]

ODAK = "Samsun ilinin Ladik ilcesi"

# ONEMLI: Sadece son bu kadar SAAT icinde yayimlanan haberler alinir.
# Eski haber gelmesini engelleyen ayar budur. Istersen 24 yap (daha sıkı).
MAX_YAS_SAAT = 48

SEEN_FILE = "gorulen_haberler.json"
MAX_SEEN = 800
SIM_ESIK = 0.82

KATEGORI_EMOJI = {
    "kaza-asayis": "🚨",
    "yol-hava-doga": "🌧",
    "belediye-resmi": "🏛",
    "etkinlik-kultur": "🎉",
    "spor": "⚽",
    "diger": "📰",
}


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_seen(lst):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(lst[-MAX_SEEN:], f, ensure_ascii=False)


def normalize_baslik(t):
    t = t.lower()
    t = re.sub(r"\s*[-–|]\s*[^-–|]+$", "", t)
    t = re.sub(r"[^0-9a-zçğıöşü ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def thash(t):
    return "t:" + hashlib.md5(normalize_baslik(t).encode("utf-8")).hexdigest()[:16]


def yayim_zamani(e):
    """RSS girdisinin yayim zamanini (UTC saniye) dondurur; yoksa None."""
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            try:
                return calendar.timegm(t)
            except Exception:
                pass
    return None


def yeterince_yeni(e):
    """Haber son MAX_YAS_SAAT icinde mi yayimlanmis?"""
    ts = yayim_zamani(e)
    if ts is None:
        return True  # tarih yoksa ele alma; dedup zaten tek sefer gonderir
    return (time.time() - ts) <= MAX_YAS_SAAT * 3600


def telegram(metin):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": metin,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=20)
        if r.status_code != 200:
            print("Telegram hata:", r.text[:300])
            return False
        return True
    except Exception as e:
        print("Telegram istisna:", e)
        return False


def fetch_all():
    out = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("RSS okunamadi:", url, e)
            continue
        for e in feed.entries:
            link = e.get("link", "")
            title = e.get("title", "").strip()
            if not link or not title:
                continue
            if not yeterince_yeni(e):      # <-- eski haberleri burada eliyoruz
                continue
            summary = re.sub("<[^>]+>", "", e.get("summary", "")).strip()
            src = ""
            if isinstance(e.get("source"), dict):
                src = e["source"].get("title", "")
            out.append({"url": link, "title": title, "summary": summary, "source": src})
    return out


def tekille(items):
    kept, norms = [], []
    for it in items:
        n = normalize_baslik(it["title"])
        if any(difflib.SequenceMatcher(None, n, kn).ratio() >= SIM_ESIK for kn in norms):
            continue
        kept.append(it)
        norms.append(n)
    return kept


def gemini_siniflandir(items):
    if not GEMINI_API_KEY:
        return None
    liste = "\n".join(
        f'{i}. baslik: {it["title"]} | ozet: {it["summary"][:200]}'
        for i, it in enumerate(items)
    )
    prompt = (
        f"Asagida Turkce haber basliklari var. Odak: {ODAK}.\n"
        "Her haber icin sunlari belirle:\n"
        f"- ilgili: Haber DOGRUDAN {ODAK} ile ilgili mi? "
        "Amasya'nin Ladik ilcesi, 'Ladik halisi' (hali turu), kisi/marka adlari "
        "gibi alakasiz seyleri ilgili SAYMA (false yap).\n"
        "- kategori: kaza-asayis | yol-hava-doga | belediye-resmi | "
        "etkinlik-kultur | spor | diger\n"
        "- onem: 1-5 (5 = cok onemli/acil: sel, kaza, can kaybi, yol kapanmasi)\n"
        "- ozet: en fazla bir cumle, Turkce.\n\n"
        f"Haberler:\n{liste}\n\n"
        'SADECE su formatta JSON dizisi dondur, baska hicbir sey yazma: '
        '[{"i":0,"ilgili":true,"kategori":"kaza-asayis","onem":4,"ozet":"..."}]'
    )
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    try:
        r = requests.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }, timeout=60)
        if r.status_code != 200:
            print("Gemini hata:", r.status_code, r.text[:300])
            return None
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        print("Gemini istisna:", e)
        return None


def kural_filtre(it):
    m = (it["title"] + " " + it["summary"]).lower()
    if "ladik" not in m:
        return False
    if "amasya" in m and "samsun" not in m:
        return False
    return True


def mesaj(it, karar):
    emoji = KATEGORI_EMOJI.get(karar.get("kategori", ""), "📰")
    ozet = html.escape(karar.get("ozet") or it["title"])
    bas = f"{emoji} <b>{ozet}</b>"
    if karar.get("onem", 0) >= 5:
        bas = "⚡ <b>ACİL</b>\n" + bas
    if it.get("source"):
        return f"{bas}\n🗞 {html.escape(it['source'])}\n{it['url']}"
    return f"{bas}\n{it['url']}"


def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("HATA: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secret eksik.")
        sys.exit(1)

    seen_list = load_seen()
    seen = set(seen_list)

    def mark(it):
        for k in (it["url"], thash(it["title"])):
            if k not in seen:
                seen.add(k)
                seen_list.append(k)

    ilk_calisma = len(seen) == 0
    items = fetch_all()

    if ilk_calisma:
        for it in items:
            mark(it)
        save_seen(seen_list)
        telegram("✅ Akıllı Ladik haber botu aktif. "
                 "Sadece yeni haberler süzülüp buraya düşecek.")
        print(f"Ilk calisma: {len(items)} haber kaydedildi (bildirim yok).")
        return

    aday = [it for it in items
            if it["url"] not in seen and thash(it["title"]) not in seen]
    aday = tekille(aday)

    if not aday:
        print("Yeni aday yok.")
        return

    kararlar = gemini_siniflandir(aday)

    idx_map = {}
    if kararlar is None:
        print("Gemini devre disi; kural filtresine dusuldu.")
        for i, it in enumerate(aday):
            if kural_filtre(it):
                idx_map[i] = {"ilgili": True, "kategori": "diger",
                              "onem": 3, "ozet": it["title"]}
    else:
        for d in kararlar:
            i = d.get("i")
            if isinstance(i, int) and d.get("ilgili"):
                idx_map[i] = d

    gonderilen = 0
    for i, it in enumerate(aday):
        mark(it)
        if i in idx_map:
            if telegram(mesaj(it, idx_map[i])):
                gonderilen += 1
                time.sleep(1)

    save_seen(seen_list)
    print(f"{gonderilen} haber gonderildi "
          f"({len(aday)} aday, {len(idx_map)} ilgili bulundu).")


if __name__ == "__main__":
    main()
