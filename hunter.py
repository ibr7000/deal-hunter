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
RESULTS_PER_PRODUCT = 5          # نفحص مصادر أكثر لأن المتاجر مفتوحة الآن

# ===== إعدادات قابلة للتعديل =====
MIN_DROP_PERCENT = 15            # أقل نسبة انخفاض (مقابل أقل سعر تاريخي) تستحق تنبيهاً
MAX_DROP_PERCENT = 70            # انخفاض أكبر = مشبوه، يُتجاهل
MIN_READINGS_FOR_ALERT = 3       # لا تنبيه قبل بناء تاريخ كافٍ
MIN_CONFIDENCE = 6               # درجة ثقة الذكاء الاصطناعي (من 10) لقبول السعر
OUTLIER_LOW_RATIO = 0.45         # داخل الجولة: ارفض ما هو أقل من 45% من وسيط الجولة
OUTLIER_HIGH_RATIO = 2.5         # وارفض ما هو أعلى من 2.5 ضعف الوسيط
SUSPICIOUS_VS_HISTORY = 0.4      # سعر أقل من 40% من المعتاد تاريخياً = خطأ
# ================================

# مواقع ليست متاجر (مصدر الأرقام الوهمية) — تُرفض دائماً
BLOCK_DOMAINS = (
    "wikipedia.org", "youtube.com", "reddit.com", "facebook.com", "twitter.com",
    "x.com", "instagram.com", "tiktok.com", "pinterest.com", "quora.com",
    "blog", "forum", "news", "article", "wordpress", "medium.com",
    "yallabanggood", "pricena", "yaoota", "shopup", "priceza",  # مواقع مقارنة أسعار
)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

client = Groq(api_key=GROQ_API_KEY)

EXTRACT_SYSTEM = (
    "أنت محلل مشتريات خبير بالسوق السعودي. مهمتك قراءة صفحة متجر واستخراج بيانات الشراء بدقة. "
    "قواعد صارمة:\n"
    "1) السعر المطلوب هو السعر الحالي الإجمالي للدفع الكاش بالريال السعودي (SAR).\n"
    "2) إن وُجد سعر قبل الخصم (مشطوب) وسعر حالي، فالسعر الحالي هو الأقل، وسجّل القديم في original_price.\n"
    "3) ⚠️ تجاهل تماماً أسعار التقسيط (قسط، تابي، تمارا، Tabby، Tamara، دفعات، شهرياً، مقدم).\n"
    "4) تأكد أنه المنتج الرئيسي الكامل المطلوب، وليس إكسسواراً (شاحن، غلاف، كيبل) ولا قطعة غيار ولا موديلاً مختلفاً.\n"
    "5) is_store=true فقط إذا كانت الصفحة صفحة شراء فعلية لمتجر (فيها سعر وزر شراء/سلة)، "
    "و false إن كانت مقالة أو مقارنة أسعار أو منتدى.\n"
    "6) confidence درجة ثقتك من 0 إلى 10 في صحة السعر ومطابقة المنتج.\n"
    "أعد JSON فقط بهذا الشكل تماماً بدون أي نص إضافي:\n"
    '{"is_store": true/false, "is_main_product": true/false, "price": رقم او null, '
    '"original_price": رقم او null, "currency": "SAR", "store": "اسم المتجر", '
    '"in_stock": true/false, "confidence": رقم من 0 الى 10}\n'
    "السعر رقم فقط بدون فواصل آلاف وبدون رمز. إن لم تجد سعر كاش واضحاً اجعل price=null وconfidence=0."
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
    for attempt in range(3):
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
                        return links
            if links:
                return links
        except Exception as e:
            print(f"   تحذير: فشل البحث (محاولة {attempt + 1}): {e}")
        time.sleep(6 + attempt * 6)
    return links


def jina_read(url):
    headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
    for attempt in range(2):
        try:
            resp = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=50)
            if resp.status_code == 200:
                return resp.text[:9000]
            print(f"   تحذير: Jina كود {resp.status_code} (محاولة {attempt + 1})")
            if resp.status_code in (429, 451, 503):
                time.sleep(15)
                continue
            return None
        except Exception as e:
            print(f"   تحذير: فشل Jina (محاولة {attempt + 1}): {e}")
            time.sleep(8)
    return None


def num(x):
    try:
        v = float(re.sub(r"[^\d.]", "", str(x).replace(",", "")))
        return v if v > 0 else None
    except Exception:
        return None


def extract_offer(name, text, url):
    for attempt in range(2):
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
            d = json.loads(c.choices[0].message.content)
            price = num(d.get("price"))
            conf = d.get("confidence", 0) or 0
            try:
                conf = float(conf)
            except Exception:
                conf = 0
            # شروط القبول: متجر + منتج رئيسي + سعر صالح + ثقة كافية + متوفر
            if (d.get("is_store") and d.get("is_main_product") and price
                    and conf >= MIN_CONFIDENCE and d.get("in_stock", True)):
                orig = num(d.get("original_price"))
                discount = None
                if orig and orig > price:
                    discount = round((orig - price) / orig * 100, 1)
                return {
                    "price": price,
                    "original_price": orig,
                    "discount_pct": discount,
                    "currency": str(d.get("currency", "SAR")).strip() or "SAR",
                    "store": str(d.get("store", "")).strip() or "متجر",
                    "confidence": conf,
                    "url": url,
                }
            return None
        except Exception as e:
            msg = str(e)
            print(f"   تحذير: فشل الاستخراج (محاولة {attempt + 1}): {e}")
            if "429" in msg or "rate" in msg.lower():
                time.sleep(20)
                continue
            break
    return None


def filter_outliers(offers):
    """يرفض الأسعار الشاذة جداً داخل نفس الجولة (وهمية الانخفاض أو الارتفاع)."""
    valid = [o for o in offers if o.get("price")]
    if len(valid) <= 2:
        return valid
    med = statistics.median([o["price"] for o in valid])
    return [o for o in valid
            if med * OUTLIER_LOW_RATIO <= o["price"] <= med * OUTLIER_HIGH_RATIO]


def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("   (تنبيه تليجرام غير مُفعّل)")
        return
    try:
        api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(api, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
        print("   ✅ أُرسل تنبيه تليجرام" if r.status_code == 200
              else f"   تحذير: تليجرام كود {r.status_code}")
    except Exception as e:
        print(f"   تحذير: فشل تليجرام ({e})")


def main():
    print("بدء جولة V3...")
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
            time.sleep(3 + random.random() * 3)
            if not text:
                continue
            off = extract_offer(name, text, url)
            if off:
                tag = f" (خصم {off['discount_pct']}%)" if off.get("discount_pct") else ""
                offers.append(off)
                print(f"      {off['store']}: {off['price']} SAR | ثقة {off['confidence']}{tag}")
            time.sleep(3 + random.random() * 3)

        clean = filter_outliers(offers)
        if not clean:
            prices[name]["last_status"] = "لم يُعثر على سعر موثوق"
            print("   لا يوجد سعر موثوق هذه الجولة.")
            time.sleep(4 + random.random() * 4)
            continue

        best = min(clean, key=lambda o: o["price"])

        # حارس تاريخي ضد القفزات الوهمية المنخفضة جداً
        baseline = hist_med if hist_med else hist_low
        if baseline and best["price"] < baseline * SUSPICIOUS_VS_HISTORY:
            prices[name]["last_status"] = "سعر مشبوه منخفض جداً — تم تجاهله"
            print(f"   ⚠️ {best['price']} مشبوه مقابل المعتاد {baseline} — تجاهلته.")
            time.sleep(4 + random.random() * 4)
            continue

        prices[name]["readings"].append({
            "t": now,
            "price": best["price"],
            "original_price": best.get("original_price"),
            "discount_pct": best.get("discount_pct"),
            "currency": best["currency"],
            "store": best["store"],
            "url": best["url"],
        })
        prices[name]["readings"] = prices[name]["readings"][-200:]
        prices[name]["last_status"] = "تم التحديث"

        # تنبيه ذكي: مقارنة بأقل سعر تاريخي بعد بناء تاريخ كافٍ
        if hist_low and len(hist) >= MIN_READINGS_FOR_ALERT and best["price"] < hist_low:
            drop = round((hist_low - best["price"]) / hist_low * 100, 1)
            if MIN_DROP_PERCENT <= drop <= MAX_DROP_PERCENT:
                send_telegram(
                    f"🚨 انخفاض سعر! منتج {name} نزل {drop}% عن أقل سعر سابق. "
                    f"السعر الآن {best['price']} ريال في {best['store']}. "
                    f"الرابط: {best['url']}"
                )
                print(f"   🚨 انخفاض {drop}% — أُطلق التنبيه")
            else:
                print(f"   انخفاض {drop}% خارج النطاق المنطقي — لا تنبيه.")

        time.sleep(4 + random.random() * 4)

    save_prices(prices)
    print("\nتم الحفظ في prices.json")


if __name__ == "__main__":
    main()
