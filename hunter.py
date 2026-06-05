import os
import re
import json
import time
import datetime
import requests
from ddgs import DDGS
from groq import Groq

WATCHLIST_FILE = "watchlist.txt"
DEALS_FILE = "deals.json"

MODEL = "llama-3.3-70b-versatile"   # عقل الاستخراج والتحليل (Llama 3)
RESULTS_PER_PRODUCT = 3             # عدد الروابط التي نفحصها لكل منتج
DELAY = 4                           # مهلة بين الطلبات (ثوانٍ)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")   # اختياري

client = Groq(api_key=GROQ_API_KEY)

SKIP_DOMAINS = ("wikipedia.org", "youtube.com", "reddit.com", "facebook.com",
                "twitter.com", "x.com", "instagram.com", "tiktok.com",
                "pinterest.com", "quora.com")

EXTRACT_SYSTEM = (
    "أنت محرك استخراج بيانات دقيق لأسعار المنتجات. "
    "ستستلم اسم منتج يبحث عنه المستخدم ونص صفحة متجر بصيغة ماركداون. مهمتك: "
    "(1) تأكد أن الصفحة تعرض المنتج الرئيسي نفسه المطلوب، وليست إكسسواراً مثل شاحن أو غلاف "
    "أو كيبل أو حماية شاشة، ولا منتجاً مختلفاً. "
    "(2) استخرج سعر البيع الحالي للمنتج الرئيسي، وتجاهل أسعار الأقساط أو الإكسسوارات. "
    "(3) استخرج العملة واسم المتجر. "
    "أعد JSON فقط بدون أي نص إضافي بهذا الشكل تماماً: "
    '{"is_main_product": true او false, "price": رقم او null, '
    '"currency": "نص", "store": "نص", "title": "نص"}. '
    "السعر يجب أن يكون رقماً فقط بدون فواصل آلاف وبدون رمز عملة. "
    "إن لم تكن الصفحة للمنتج الرئيسي اجعل is_main_product=false وprice=null."
)


def load_watchlist():
    items = []
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    items.append(line)
    return items


def load_products():
    if os.path.exists(DEALS_FILE):
        try:
            with open(DEALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("products", {})
        except Exception:
            return {}
    return {}


def save_output(data):
    with open(DEALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def search_links(name):
    links = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(f"{name} سعر", max_results=8):
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


def extract_offer(name, text):
    try:
        c = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"اسم المنتج المطلوب: {name}\n\nنص الصفحة:\n{text}"},
            ],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        data = json.loads(c.choices[0].message.content)
        if data.get("is_main_product") and data.get("price") is not None:
            price = float(re.sub(r"[^\d.]", "", str(data["price"]).replace(",", "")))
            return {
                "price": price,
                "currency": str(data.get("currency", "")).strip(),
                "store": str(data.get("store", "")).strip(),
                "title": str(data.get("title", "")).strip(),
            }
    except Exception as e:
        print(f"   تحذير: فشل الاستخراج ({e})")
    return None


def main():
    print("بدء جولة صيد العروض...")
    names = load_watchlist()
    products = load_products()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for name in names:
        print(f"\nالمنتج: {name}")
        offers = []
        for url in search_links(name):
            print(f"   فحص: {url}")
            text = jina_read(url)
            time.sleep(DELAY)
            if not text:
                continue
            offer = extract_offer(name, text)
            if offer:
                offer["url"] = url
                offers.append(offer)
                print(f"      {offer['store']}: {offer['price']} {offer['currency']}")
            time.sleep(DELAY)

        rec = products.get(name, {"name": name, "offers": [], "best": None, "best_history": []})
        rec["name"] = name
        rec["offers"] = offers

        valid = [o for o in offers if o.get("price")]
        if valid:
            best = min(valid, key=lambda o: o["price"])
            rec["best"] = best
            rec["best_history"].append({"t": now, "p": best["price"]})
            rec["best_history"] = rec["best_history"][-50:]
        products[name] = rec

    save_output({"updated_at": now, "products": products})
    print("\nتم الحفظ في deals.json")


if __name__ == "__main__":
    main()
