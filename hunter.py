import os
import re
import json
import time
import random
import datetime
import statistics
import requests
from ddgs import DDGS
from groq import Groq

WATCHLIST_FILE = "watchlist.txt"
PRICES_FILE = "prices.json"
MODEL = "llama-3.3-70b-versatile"

# ===== إعدادات =====
RESULTS_PER_PRODUCT = 3      # مصادر لكل منتج (أقل = طلبات Groq أقل = حجب أقل)
JINA_TIMEOUT = 40
GROQ_PAUSE = 5               # توقف بعد كل طلب Groq لاحترام الحد المجاني
GROQ_MAX_RETRY = 4           # محاولات إعادة عند RateLimit (مع انتظار متزايد)
SHORT_PAUSE = 2
# ===== الجودة والتنبيه =====
MIN_CONFIDENCE = 4
MIN_DROP_PERCENT = 15
MAX_DROP_PERCENT = 70
MIN_READINGS_FOR_ALERT = 3
OUTLIER_LOW_RATIO = 0.4
OUTLIER_HIGH_RATIO = 2.6
SUSPICIOUS_VS_HISTORY = 0.4
# ===================

BLOCK_DOMAINS = (
    "wikipedia.org", "youtube.com", "reddit.com", "facebook.com", "twitter.com",
    "x.com", "instagram.com", "tiktok.com", "pinterest.com", "quora.com",
    "/blog", "forum", "wordpress", "medium.com",
    "pricena", "yaoota", "priceza",
)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

client = Groq(api_key=GROQ_API_KEY)

EXTRACT_SYSTEM = (
    "أنت محلل مشتريات خبير بالسوق السعودي. اقرأ صفحة متجر واستخرج بيانات الشراء. قواعد:\n"
    "1) السعر المطلوب هو السعر الحالي الإجمالي للدفع الكاش بالريال السعودي (SAR).\n"
    "2) إن وُجد سعر مشطوب قبل الخصم، فالسعر الحالي هو الأقل، وسجّل القديم في original_price.\n"
    "3) ⚠️ تجاهل أسعار التقسيط (قسط، تابي، تمارا، Tabby، Tamara، دفعات، شهرياً، مقدم).\n"
    "4) تأكد أنه المنتج المطلوب تقريباً وليس إكسسواراً واضحاً (شاحن/غلاف/كيبل) ولا قطعة غيار.\n"
    "5) is_store=true إذا بدت صفحة بيع فيها سعر، و false فقط لو كانت بوضوح مقالة أو منتدى بلا سعر.\n"
    "6) confidence درجة ثقتك من 0 إلى 10. إن وجدت سعراً واضحاً للمنتج اجعلها 6 أو أكثر.\n"
    "أعد JSON فقط بهذا الشكل بدون أي نص إضافي:\n"
    '{"is_store": true/false, "is_main_product": true/false, "price": رقم او null, '
    '"original_price": رقم او null, "currency": "SAR", "store": "اسم المتجر", '
    '"in_stock": true/false, "confidence": رقم من 0 الى 10}\n'
    "السعر رقم فقط بدون فواصل آلاف وبدون رمز. إن لم تجد أي سعر للمنتج اجعل price=null."
)


def load_watchlist():
    items = []
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        items.append(line)
    except Exception as e:
        print(f"تحذير: تعذّر قراءة القائمة ({e})")
    return items


def load_prices():
    try:
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"تحذير: تعذّر قراءة {PRICES_FILE} ({e})")
    return {}


def save_prices(data):
    try:
        with open(PRICES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"تحذير: تعذّر حفظ {PRICES_FILE} ({e})")


def looks_blocked(url):
    low = url.lower()
    return any(b in low for b in BLOCK_DOMAINS)


def search_links(name):
    seen, links = set(), []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"{name} السعودية شراء سعر", max_results=30):
                url = r.get("href") or r.get("url")
                if not url or looks_blocked(url):
                    continue
                base = url.split("?")[0]
                if base in seen:
                    continue
                seen.add(base)
                links.append(url)
                if len(links) >= RESULTS_PER_PRODUCT:
                    break
    except Exception as e:
        print(f"   تحذير: فشل البحث ({e})")
    return links


def jina_read(url):
    headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
    try:
        resp = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=JINA_TIMEOUT)
        if resp.status_code == 200:
            return resp.text[:9000]
        print(f"   Jina كود {resp.status_code}")
    except Exception as e:
        print(f"   تخطٍّ Jina ({type(e).__name__})")
    return None


def num(x):
    try:
        v = float(re.sub(r"[^\d.]", "", str(x).replace(",", "")))
        return v if v > 0 else None
    except Exception:
        return None


def call_groq(name, text, url):
    """يستدعي Groq مع إعادة محاولة ذكية عند تجاوز الحد المجاني (RateLimit)."""
    for attempt in range(GROQ_MAX_RETRY):
        try:
            c = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": f"اسم المنتج المطلوب: {name}\n\nرابط الصفحة: {url}\n\nنص الصفحة:\n{text}"},
                ],
                temperature=0,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            return c.choices[0].message.content
        except Exception as e:
            msg = str(e).lower()
            if "rate" in msg or "429" in msg:
                wait = 20 * (attempt + 1)   # 20، 40، 60، 80 ثانية
                print(f"   Groq مزدحم، انتظار {wait}ث ثم إعادة (محاولة {attempt + 1})...")
                time.sleep(wait)
                continue
            print(f"   خطأ Groq ({type(e).__name__})")
            return None
    print("   تعذّر الاستخراج بعد عدة محاولات (الحد المجاني).")
    return None


def extract_offer(name, text, url):
    raw = call_groq(name, text, url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        price = num(d.get("price"))
        try:
            conf = float(d.get("confidence", 0) or 0)
        except Exception:
            conf = 0
        if price and conf >= MIN_CONFIDENCE and d.get("is_store", True):
            orig = num(d.get("original_price"))
            discount = round((orig - price) / orig * 100, 1) if (orig and orig > price) else None
            return {
                "price": price, "original_price": orig, "discount_pct": discount,
                "currency": str(d.get("currency", "SAR")).strip() or "SAR",
                "store": str(d.get("store", "")).strip() or "متجر",
                "confidence": conf, "url": url,
            }
    except Exception:
        print("   تعذّر تحليل رد Groq.")
    return None


def filter_outliers(offers):
    valid = [o for o in offers if o.get("price")]
    if len(valid) <= 2:
        return valid
    med = statistics.median([o["price"] for o in valid])
    return [o for o in valid if med * OUTLIER_LOW_RATIO <= o["price"] <= med * OUTLIER_HIGH_RATIO]


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("   (تنبيه تليجرام غير مُفعّل)")
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(api, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
        print("   ✅ أُرسل تنبيه تليجرام" if r.status_code == 200 else f"   تحذير: تليجرام {r.status_code}")
    except Exception as e:
        print(f"   تحذير: فشل تليجرام ({e})")


def main():
    print("بدء جولة V3.2 (إيقاع هادئ لـ Groq)...")
    names = load_watchlist()
    prices = load_prices()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    for name in names:
        print(f"\nالمنتج: {name}")
        if name not in prices or not isinstance(prices[name], dict):
            prices[name] = {"readings": []}
        prices[name].setdefault("readings", [])
        prices[name]["last_checked"] = now

        old = prices[name]["readings"]
        hist = [r["price"] for r in old if r.get("price")]
        hist_low = min(hist) if hist else None
        hist_med = statistics.median(hist) if hist else None

        offers = []
        for url in search_links(name):
            print(f"   فحص: {url}")
            text = jina_read(url)
            time.sleep(SHORT_PAUSE)
            if not text:
                continue
            off = extract_offer(name, text, url)
            if off:
                tag = f" (خصم {off['discount_pct']}%)" if off.get("discount_pct") else ""
                offers.append(off)
                print(f"      {off['store']}: {off['price']} SAR | ثقة {off['confidence']}{tag}")
            time.sleep(GROQ_PAUSE)   # تهدئة الإيقاع بين طلبات Groq

        clean = filter_outliers(offers)
        if not clean:
            prices[name]["last_status"] = "لم يُعثر على سعر موثوق"
            print("   لا يوجد سعر موثوق هذه الجولة.")
            continue

        best = min(clean, key=lambda o: o["price"])
        baseline = hist_med if hist_med else hist_low
        if baseline and best["price"] < baseline * SUSPICIOUS_VS_HISTORY:
            prices[name]["last_status"] = "سعر مشبوه منخفض جداً — تم تجاهله"
            print(f"   ⚠️ {best['price']} مشبوه مقابل المعتاد {baseline} — تجاهلته.")
            continue

        prices[name]["readings"].append({
            "t": now, "price": best["price"], "original_price": best.get("original_price"),
            "discount_pct": best.get("discount_pct"), "currency": best["currency"],
            "store": best["store"], "url": best["url"],
        })
        prices[name]["readings"] = prices[name]["readings"][-200:]
        prices[name]["last_status"] = "تم التحديث"

        if hist_low and len(hist) >= MIN_READINGS_FOR_ALERT and best["price"] < hist_low:
            drop = round((hist_low - best["price"]) / hist_low * 100, 1)
            if MIN_DROP_PERCENT <= drop <= MAX_DROP_PERCENT:
                send_telegram(
                    f"🚨 انخفاض سعر! منتج {name} نزل {drop}% عن أقل سعر سابق. "
                    f"السعر الآن {best['price']} ريال في {best['store']}. الرابط: {best['url']}"
                )
                print(f"   🚨 انخفاض {drop}% — أُطلق التنبيه")

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
