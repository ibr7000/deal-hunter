import os
import re
import json
import time
import datetime
import requests
from ddgs import DDGS
from groq import Groq

WATCHLIST_FILE = "watchlist.txt"
PRICES_FILE = "prices.json"

MODEL = "llama-3.3-70b-versatile"
RESULTS_PER_PRODUCT = 3
DELAY = 4
DROP_ALERT_PERCENT = 20            # نسبة الانخفاض التي تُطلق التنبيه

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

client = Groq(api_key=GROQ_API_KEY)

SKIP_DOMAINS = ("wikipedia.org", "youtube.com", "reddit.com", "facebook.com",
                "twitter.com", "x.com", "instagram.com", "tiktok.com",
                "pinterest.com", "quora.com")

EXTRACT_SYSTEM = (
    "أنت محلل مالي خبير بالسوق السعودي. استخرج السعر الإجمالي بالريال السعودي (SAR) فقط. "
    "⚠️ تحذير حرج: تجاهل تماماً أي أسعار مقترنة بكلمات (قسط، تابي، تمارا، Tabby، Tamara، دفعات، شهرياً). "
    "أريد السعر الكاش الإجمالي فقط. "
    "تأكد أيضاً أن الصفحة للمنتج الرئيسي نفسه وليست إكسسواراً (شاحن، غلاف، كيبل، حماية شاشة). "
    "استخرج البيانات بصيغة JSON فقط بدون أي نص إضافي بهذا الشكل تماماً: "
    '{"is_main_product": true او false, "price": رقم فقط او null, '
    '"currency": "SAR", "store": "اسم المتجر", "url": "رابط الصفحة"}. '
    "السعر رقم فقط بدون فواصل آلاف وبدون رمز عملة. "
    "إن لم تجد سعراً كاش واضحاً للمنتج الرئيسي اجعل is_main_product=false وprice=null."
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
        print(f"تحذير: تعذّر قراءة قائمة المراقبة ({e})")
    return items


def load_prices():
    """يحمّل السجل التاريخي. الشكل: {اسم المنتج: {"readings": [...]}}"""
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


def search_links(name):
    links = []
    try:
        query = f"{name} السعودية سعر شراء"
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=8):
                url = r.get("href") or r.get("url")
                if not url or any(d in url for d in SKIP_DOMAINS):
                    continue
                links.append(url)
                if len(links) >= RESULTS_PER_PRODUCT:
                    break
    except Exception as e:
        print(f"   تحذير: فشل البحث ({e})")
    return links


def jina_read(url):
    headers = {}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        resp = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=45)
        if resp.status_code == 200:
            return resp.text[:8000]
        print(f"   تحذير: Jina كود {resp.status_code}")
    except Exception as e:
        print(f"   تحذير: فشل Jina ({e})")
    return None


def extract_offer(name, text, url):
    try:
        c = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"اسم المنتج المطلوب: {name}\n\nرابط الصفحة: {url}\n\nنص الصفحة:\n{text}"},
            ],
            temperature=0,
            max_tokens=250,
            response_format={"type": "json_object"},
        )
        data = json.loads(c.choices[0].message.content)
        if data.get("is_main_product") and data.get("price") is not None:
            price = float(re.sub(r"[^\d.]", "", str(data["price"]).replace(",", "")))
            if price <= 0:
                return None
            return {
                "price": price,
                "currency": str(data.get("currency", "SAR")).strip() or "SAR",
                "store": str(data.get("store", "")).strip() or "متجر",
                "url": str(data.get("url", "")).strip() or url,
            }
    except Exception as e:
        print(f"   تحذير: فشل الاستخراج ({e})")
    return None


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("   (تنبيه تليجرام غير مُفعّل: المتغيرات فارغة)")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code == 200:
            print("   ✅ أُرسل تنبيه تليجرام")
        else:
            print(f"   تحذير: تليجرام كود {r.status_code} - {r.text[:150]}")
    except Exception as e:
        print(f"   تحذير: فشل إرسال تليجرام ({e})")


def main():
    print("بدء جولة صيد العروض V2...")
    names = load_watchlist()
    prices = load_prices()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

    for name in names:
        print(f"\nالمنتج: {name}")

        # أفضل (أقل) سعر تاريخي مسجل قبل هذه الجولة
        old_readings = prices.get(name, {}).get("readings", [])
        old_prices = [r["price"] for r in old_readings if r.get("price")]
        historic_low = min(old_prices) if old_prices else None

        # جمع عروض هذه الجولة
        offers = []
        for url in search_links(name):
            print(f"   فحص: {url}")
            text = jina_read(url)
            time.sleep(DELAY)
            if not text:
                continue
            offer = extract_offer(name, text, url)
            if offer:
                offers.append(offer)
                print(f"      {offer['store']}: {offer['price']} {offer['currency']}")
            time.sleep(DELAY)

        if not offers:
            print("   لم أجد سعراً موثوقاً في هذه الجولة.")
            # نحافظ على السجل القديم كما هو
            if name not in prices:
                prices[name] = {"readings": []}
            continue

        best = min(offers, key=lambda o: o["price"])

        # إضافة قراءة جديدة دون مسح القديم (Time-Series)
        if name not in prices:
            prices[name] = {"readings": []}
        prices[name]["readings"].append({
            "t": now,
            "price": best["price"],
            "currency": best["currency"],
            "store": best["store"],
            "url": best["url"],
        })
        prices[name]["readings"] = prices[name]["readings"][-200:]  # حد أقصى للحجم

        # منطق التنبيه: مقارنة بأقل سعر تاريخي
        if historic_low and best["price"] < historic_low:
            drop = round((historic_low - best["price"]) / historic_low * 100, 1)
            if drop >= DROP_ALERT_PERCENT:
                msg = (
                    f"🚨 انهيار في السعر! منتج {name} نزل بنسبة {drop}%. "
                    f"السعر الآن {best['price']} ريال في {best['store']}. "
                    f"رابط الشراء: {best['url']}"
                )
                send_telegram(msg)
                print(f"   🚨 انخفاض {drop}% — أُطلق التنبيه")

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
