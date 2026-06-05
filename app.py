import os
import json
import base64
import requests
import streamlit as st

st.set_page_config(page_title="صياد العروض", page_icon="🛒", layout="centered")
st.markdown("<style>.stApp{direction:rtl;text-align:right;}</style>", unsafe_allow_html=True)

WATCHLIST_FILE = "watchlist.txt"
DEALS_FILE = "deals.json"


def secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.environ.get(key, default)


GITHUB_TOKEN = secret("GITHUB_TOKEN")
GITHUB_REPO = secret("GITHUB_REPO")
GITHUB_BRANCH = secret("GITHUB_BRANCH", "main")


def read_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def save_watchlist_to_github(text):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return False, "لم يتم ضبط GITHUB_TOKEN أو GITHUB_REPO في الإعدادات."
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{WATCHLIST_FILE}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    sha = None
    r = requests.get(api, headers=headers, params={"ref": GITHUB_BRANCH})
    if r.status_code == 200:
        sha = r.json().get("sha")
    payload = {
        "message": "تحديث قائمة المراقبة من التطبيق",
        "content": base64.b64encode(text.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    put = requests.put(api, headers=headers, json=payload)
    if put.status_code in (200, 201):
        return True, "تم الحفظ. سيُفحص في الجولة القادمة."
    return False, f"فشل الحفظ: {put.status_code}"


def read_deals():
    if os.path.exists(DEALS_FILE):
        try:
            with open(DEALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


st.title("🛒 صياد العروض الذكي")
deals = read_deals()
if deals.get("updated_at"):
    st.caption(f"آخر تحديث: {deals['updated_at']}")

tab1, tab2 = st.tabs(["📋 المنتجات", "🔥 العروض"])

with tab1:
    st.write("اكتب اسم منتج واحد في كل سطر:")
    text = st.text_area("قائمتك:", value=read_watchlist(), height=240,
                        placeholder="MacBook Pro M3\nGalaxy S25 Ultra")
    if st.button("💾 حفظ القائمة", use_container_width=True):
        ok, msg = save_watchlist_to_github(text)
        (st.success if ok else st.error)(msg)
        if not GITHUB_TOKEN:
            st.info("بدون توكن GitHub عدّل ملف watchlist.txt مباشرة من المستودع.")

with tab2:
    products = deals.get("products", {})
    if not products:
        st.info("لا توجد نتائج بعد. ستظهر بعد أول تشغيل للمحرك.")
    for name, p in products.items():
        with st.container(border=True):
            st.markdown(f"### {name}")
            best = p.get("best")
            if best:
                st.metric(f"أفضل سعر ({best.get('store', '')})",
                          f"{best.get('price')} {best.get('currency', '')}")
                hist = [h["p"] for h in p.get("best_history", [])]
                if len(hist) >= 2:
                    if best["price"] <= min(hist):
                        st.success("🎯 هذا أقل سعر سُجّل!")
                    st.line_chart(hist, height=140)
                st.markdown("**كل العروض:**")
                for o in p.get("offers", []):
                    st.markdown(f"- {o.get('store', 'متجر')}: {o.get('price')} "
                                f"{o.get('currency', '')} — [الرابط]({o.get('url', '#')})")
            else:
                st.warning("لم أجد سعراً موثوقاً بعد لهذا المنتج.")
