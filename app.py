from datetime import date, datetime
import os
import re

import altair as alt
import pandas as pd
from sqlalchemy import create_engine, text
import streamlit as st

TARGET = 1_300_000

st.set_page_config(page_title="給料トラッカー", page_icon="💰", layout="centered")
st.markdown(
    "<h1 style='text-align: center;'>給料トラッカー</h1>", unsafe_allow_html=True
)


# ===== データベース接続 =====
def get_db_url() -> str:
    # Streamlit Secrets → 環境変数 → ローカルSQLite の順で接続文字列を取得
    db_url = None
    try:
        if "DATABASE_URL" in st.secrets:
            db_url = st.secrets["DATABASE_URL"]
        elif "postgres" in st.secrets and "url" in st.secrets["postgres"]:
            db_url = st.secrets["postgres"]["url"]
    except Exception:
        # secrets.toml が無い環境
        pass
    if not db_url:
        db_url = os.environ.get("DATABASE_URL", "sqlite:///salaries.db")

    # ドライバを psycopg (v3) に固定（SQLAlchemyのバージョンで既定ドライバが変わるため）
    for prefix in ("postgres://", "postgresql://"):
        if db_url.startswith(prefix):
            db_url = "postgresql+psycopg://" + db_url[len(prefix):]
            break
    return db_url


@st.cache_resource
def get_engine():
    # pool_pre_ping: アイドル中にサーバー側で切られた接続を使う前に検知して張り直す
    return create_engine(get_db_url(), pool_pre_ping=True)


def is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


def init_db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS salaries (
                month VARCHAR(7) PRIMARY KEY,
                salary INTEGER NOT NULL,
                updated_at VARCHAR(30) NOT NULL
            )
            """
            )
        )


def upsert_month(month: str, salary: int):
    now = datetime.now().isoformat(timespec="seconds")
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            INSERT INTO salaries (month, salary, updated_at)
            VALUES (:month, :salary, :updated_at)
            ON CONFLICT(month) DO UPDATE SET
                salary = EXCLUDED.salary,
                updated_at = EXCLUDED.updated_at
            """
            ),
            {"month": month, "salary": int(salary), "updated_at": now},
        )


def delete_month(month: str):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM salaries WHERE month = :month"), {"month": month}
        )


def load_data() -> pd.DataFrame:
    engine = get_engine()
    try:
        with engine.connect() as conn:
            df = pd.read_sql_query(
                text(
                    "SELECT month, salary, updated_at FROM salaries ORDER BY month"
                ),
                conn,
            )
    except Exception as e:
        # 空表示にすると「データが消えた」ように見えるので，エラーとして止める
        st.error(f"データの読み込みに失敗しました：{e}")
        st.stop()

    if df.empty:
        return df
    df["month"] = df["month"].astype(str)
    df["salary"] = (
        pd.to_numeric(df["salary"], errors="coerce").fillna(0).astype(int)
    )
    return df


def auto_backup_csv(df: pd.DataFrame):
    if df.empty:
        return

    os.makedirs("backup", exist_ok=True)
    path = "backup/salary_backup_latest.csv"
    df.sort_values("month").to_csv(path, index=False, encoding="utf-8-sig")


def build_timeseries(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["month_date"] = pd.to_datetime(
        out["month"] + "-01", format="%Y-%m-%d", errors="coerce"
    )
    out = (
        out.dropna(subset=["month_date"])
        .sort_values("month_date")
        .reset_index(drop=True)
    )
    out["year"] = out["month_date"].dt.year.astype(int)
    out["ym"] = out["month_date"].dt.strftime("%Y-%m")
    out["cumulative"] = out["salary"].cumsum()
    return out


def yearly_summary(ts: pd.DataFrame) -> pd.DataFrame:
    if ts.empty:
        return ts
    g = ts.groupby("year", as_index=False)
    year_sum = g["salary"].sum().rename(columns={"salary": "year_total"})
    year_avg = g["salary"].mean().rename(columns={"salary": "month_avg"})
    year_max = g["salary"].max().rename(columns={"salary": "max_month_salary"})
    out = year_sum.merge(year_avg, on="year").merge(year_max, on="year")
    out["month_avg"] = out["month_avg"].round(0).astype(int)
    out["year_total"] = out["year_total"].astype(int)
    out["max_month_salary"] = out["max_month_salary"].astype(int)
    return out.sort_values("year", ascending=False).reset_index(drop=True)


# ===== 給与明細（Bulas Payslip Mobile）から取り込み =====
# 明細の詳細画面でブックマークレットを実行すると，
# ?pm=YYYY-MM&pn=差引支給額&pt=種別 を付けてこのアプリが開く
BOOKMARKLET_JS = r"""
(function(){
var cells=[].slice.call(document.querySelectorAll('td'));
var leaf=function(td){return !td.querySelector('td');};
var monthTd=cells.find(function(td){return leaf(td)&&/^\d{4}年\s*\d{1,2}月$/.test(td.textContent.trim());});
var netTd=cells.find(function(td){return leaf(td)&&td.textContent.trim()==='差引支給額';});
var valTd=netTd&&netTd.nextElementSibling;
var net=valTd?parseInt(valTd.textContent.replace(/[^0-9-]/g,''),10):NaN;
if(!monthTd||isNaN(net)){alert('給与明細の詳細画面（差引支給額が表示されている画面）で実行してください');return;}
var m=monthTd.textContent.trim().match(/(\d{4})年\s*(\d{1,2})月/);
var typeTd=monthTd.nextElementSibling;
var type=typeTd?typeTd.textContent.trim():'';
if(!confirm(m[1]+'年'+m[2]+'月（'+(type||'給与')+'）の差引支給額 '+net.toLocaleString()+'円 を給料トラッカーに送りますか？'))return;
location.href='__APP_URL__?pm='+m[1]+'-'+('0'+m[2]).slice(-2)+'&pn='+net+'&pt='+encodeURIComponent(type);
})();
"""


def get_app_url() -> str:
    # Secrets の APP_URL → 開いているページのURL の順で決める
    try:
        if "APP_URL" in st.secrets:
            return str(st.secrets["APP_URL"]).rstrip("/") + "/"
    except Exception:
        pass
    url = getattr(st.context, "url", None) or ""
    url = url.split("?")[0].split("#")[0]
    # Streamlit Community Cloud の内部パスを外す
    url = re.sub(r"/~/\+/?$", "/", url)
    if not url:
        return ""
    return url if url.endswith("/") else url + "/"


def make_bookmarklet(app_url: str) -> str:
    code = "".join(line.strip() for line in BOOKMARKLET_JS.strip().splitlines())
    code = code.replace("__APP_URL__", app_url)
    # 日本語は \uXXXX にして，ブックマークのURL欄でも崩れないようにする
    return "javascript:" + code.encode("ascii", "backslashreplace").decode("ascii")


def payslip_import(df: pd.DataFrame):
    params = st.query_params
    pm, pn, pt = params.get("pm"), params.get("pn"), params.get("pt", "")
    if not pm or pn is None:
        return

    def close():
        st.query_params.clear()
        st.rerun()

    valid = (
        re.fullmatch(r"\d{4}-\d{2}", pm)
        and 1 <= int(pm[5:]) <= 12
        and re.fullmatch(r"-?\d+", pn)
    )
    if not valid:
        st.error("給与明細のデータを読み取れませんでした．")
        if st.button("閉じる", key="payslip_close"):
            close()
        return

    net = int(pn)
    saved = df.loc[df["month"] == pm, "salary"] if not df.empty else []
    existing = int(saved.iloc[0]) if len(saved) else None
    is_bonus = "賞与" in pt

    # 賞与は確認なしで，その月の保存済みの金額に足す
    if is_bonus:
        upsert_month(pm, (existing or 0) + net)
        st.session_state["saved_message"] = (
            f"{int(pm[:4])}年{int(pm[5:])}月に賞与 {net:,} 円を足しました"
        )
        close()

    with st.container(border=True):
        st.subheader("🧾 給与明細の取り込み")
        st.write(f"**{int(pm[:4])}年{int(pm[5:])}月**（{pt or '給与'}）")
        st.metric("差引支給額（円）", f"{net:,}")

        new_value = net
        if existing is not None:
            st.caption(f"この月の保存済み：{existing:,} 円（保存すると上書きされます）")

        c1, c2 = st.columns(2)
        if c1.button("保存", type="primary", width="stretch", key="payslip_save"):
            upsert_month(pm, new_value)
            st.session_state["saved_month"] = pm
            close()
        if c2.button("やめる", width="stretch", key="payslip_cancel"):
            close()


# 初期化
init_db()
df = load_data()
auto_backup_csv(df)
payslip_import(df)

# ===== サイドバー：表示設定 =====
st.sidebar.header("表示設定")

compact = st.sidebar.toggle("📱 コンパクト表示（スマホ推奨）", value=True)
show_table = st.sidebar.toggle("🧾 履歴テーブルを表示", value=not compact)

st.sidebar.divider()
# ===== サイドバー：バックアップ（CSV） =====
st.sidebar.subheader("バックアップ（CSV）")

if df.empty:
    st.sidebar.caption("データがないためバックアップできません．")
else:
    export_df = df.sort_values("month").copy()
    csv_bytes = export_df.to_csv(index=False).encode("utf-8-sig")
    st.sidebar.download_button(
        label="📤 CSVバックアップをダウンロード",
        data=csv_bytes,
        file_name="salary_backup.csv",
        mime="text/csv",
        width="stretch",
    )

st.sidebar.caption("復元は「このアプリから出したCSV」を使ってください．")
uploaded = st.sidebar.file_uploader(
    "📥 CSVから復元", type=["csv"], accept_multiple_files=False
)

if uploaded is not None:
    try:
        restore_df = pd.read_csv(uploaded)
        if not {"month", "salary"}.issubset(set(restore_df.columns)):
            st.sidebar.error(
                "CSV形式が違います（month, salary 列が必要）"
            )
        else:
            if "updated_at" not in restore_df.columns:
                restore_df["updated_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )

            restore_df["month"] = restore_df["month"].astype(str)
            restore_df["salary"] = (
                pd.to_numeric(restore_df["salary"], errors="coerce")
                .fillna(0)
                .astype(int)
            )
            restore_df["updated_at"] = restore_df["updated_at"].astype(str)

            engine = get_engine()
            with engine.begin() as conn:
                for _, r in restore_df.iterrows():
                    conn.execute(
                        text(
                            """
                        INSERT INTO salaries (month, salary, updated_at)
                        VALUES (:month, :salary, :updated_at)
                        ON CONFLICT(month) DO UPDATE SET
                            salary = EXCLUDED.salary,
                            updated_at = EXCLUDED.updated_at
                        """
                        ),
                        {
                            "month": str(r["month"]),
                            "salary": int(r["salary"]),
                            "updated_at": str(r["updated_at"]),
                        },
                    )

            st.sidebar.success("復元しました．画面を更新します．")
            st.rerun()

    except Exception as e:
        st.sidebar.error(f"復元に失敗しました：{e}")

st.sidebar.divider()
# ===== サイドバー：給与明細から取り込み =====
st.sidebar.subheader("給与明細から取り込み")
app_url = get_app_url()
if app_url:
    st.sidebar.caption(
        "① 下のコードをコピー（右上のボタン）\n\n"
        "② ブラウザでこのページをブックマークし，名前を「給料トラッカーに送る」にする\n\n"
        "③ ブックマークを編集して，URL欄をコピーしたコードに置き換える\n\n"
        "④ Payポータルで給与明細の詳細画面を開き，そのブックマークをタップ"
    )
    st.sidebar.code(make_bookmarklet(app_url), language=None, wrap_lines=True)
else:
    st.sidebar.caption("Secrets に APP_URL（このアプリのURL）を設定すると表示されます．")

st.sidebar.divider()
if is_sqlite():
    st.sidebar.warning(
        "保存先：SQLite（ローカルファイル）\n\n"
        "クラウド上ではアプリの再起動・スリープでデータが消えます．"
        "Secrets に DATABASE_URL を設定してください．"
    )
else:
    st.sidebar.caption(f"保存先：{get_engine().dialect.name}")

def nice_upper(max_value: int, floor: int, step: int) -> int:
    # 目盛り幅 step 単位で切り上げた上限（最低でも floor）
    upper = max(int(max_value), floor)
    return -(-upper // step) * step


today = date.today()
ts = build_timeseries(df)

# 保存直後の再実行で通知を出す
if "saved_month" in st.session_state:
    st.toast(f"{st.session_state.pop('saved_month')} を保存しました", icon="✅")
if "saved_message" in st.session_state:
    st.toast(st.session_state.pop("saved_message"), icon="✅")

# ===== 表示する年（全タブ共通） =====
data_years = set(ts["year"].unique().tolist()) if not ts.empty else set()
years = sorted(data_years | {today.year}, reverse=True)
selected_year = st.selectbox("表示する年", years, index=years.index(today.year))

year_ts = (
    ts[ts["year"] == selected_year].copy() if not ts.empty else pd.DataFrame()
)
year_total = int(year_ts["salary"].sum()) if not year_ts.empty else 0
diff = TARGET - year_total
progress = min(max(year_total / TARGET, 0.0), 1.0)

# ===== 進捗（ファーストビュー） =====
st.progress(progress, text=f"{selected_year}年 達成率 {progress * 100:.1f}%")
a, b = st.columns(2)
a.metric("合計（円）", f"{year_total:,}")
if diff >= 0:
    b.metric("残り（円）", f"{diff:,}")
else:
    b.metric("超過（円）", f"{abs(diff):,}")

# ===== タブ =====
tab1, tab2, tab3, tab4 = st.tabs(["📋 月別", "📅 年集計", "📈 グラフ", "🧾 履歴"])

with tab1:
    if year_ts.empty:
        st.info(f"{selected_year}年のデータがありません．")
    else:
        year_ts["month_num"] = year_ts["month_date"].dt.month.astype(int)

        base = pd.DataFrame({"月": list(range(1, 13))})
        merged = base.merge(
            year_ts[["month_num", "salary"]].rename(
                columns={"month_num": "月", "salary": "給料（円）"}
            ),
            on="月",
            how="left",
        )

        def fmt(x):
            if pd.isna(x):
                return ""
            return f"{int(x):,}"

        display_df = merged.copy()
        display_df["給料（円）"] = display_df["給料（円）"].apply(fmt)

        st.dataframe(display_df, width="stretch", hide_index=True)

with tab2:
    if year_ts.empty:
        st.info(f"{selected_year}年のデータがありません．")
    else:
        ys = yearly_summary(ts)
        row = ys.loc[ys["year"] == selected_year].iloc[0]
        year_total = int(row["year_total"])
        month_avg = int(row["month_avg"])
        max_month_salary = int(row["max_month_salary"])

        if compact:
            st.metric("合計（円）", f"{year_total:,}")
            st.metric("月平均（円）", f"{month_avg:,}")
            st.metric("最大月給（円）", f"{max_month_salary:,}")
        else:
            a, b, c = st.columns(3)
            a.metric("合計（円）", f"{year_total:,}")
            b.metric("月平均（円）", f"{month_avg:,}")
            c.metric("最大月給（円）", f"{max_month_salary:,}")

with tab3:
    if ts.empty:
        st.info("データがありません．")
    else:
        ys = yearly_summary(ts)

        st.markdown(
            "<h3 style='margin-top: 20px; margin-bottom: 4px;'>年内の月別推移</h3>",
            unsafe_allow_html=True,
        )

        if year_ts.empty:
            st.info(f"{selected_year}年のデータがありません．")
        else:
            graph_ts = year_ts.sort_values("month_date").reset_index(drop=True)
            graph_ts["month_label"] = graph_ts["month_date"].dt.strftime("%m")

            month_target = 200_000
            month_upper = nice_upper(
                graph_ts["salary"].max(), month_target, 20_000
            )
            month_tick_values = list(range(0, month_upper + 1, 20_000))

            month_bar = (
                alt.Chart(graph_ts)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "month_label:O", title="月", axis=alt.Axis(labelAngle=0)
                    ),
                    y=alt.Y(
                        "salary:Q",
                        title="給料（円）",
                        scale=alt.Scale(domain=[0, month_upper]),
                        axis=alt.Axis(
                            values=month_tick_values,
                            grid=True,
                            format=",.0f",
                            labelFontSize=11,
                            labelOverlap=False,
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("ym:N", title="年月"),
                        alt.Tooltip(
                            "salary:Q", title="給料（円）", format=",.0f"
                        ),
                    ],
                )
                .properties(height=320)
            )

            cap_line = (
                alt.Chart(pd.DataFrame({"y": [month_target]}))
                .mark_rule(color="red", strokeWidth=2)
                .encode(y="y:Q")
            )

            st.altair_chart(
                (month_bar + cap_line).configure_axis(
                    titleFontSize=13, labelFontSize=11
                ),
                width="stretch",
            )

        st.markdown(
            "<h3 style='margin-top: 20px; margin-bottom: 4px;'>年ごとの推移</h3>",
            unsafe_allow_html=True,
        )

        chart_df = ys.sort_values("year").copy()
        year_upper = nice_upper(chart_df["year_total"].max(), TARGET, 100_000)
        tick_values = list(range(0, year_upper + 1, 100_000))

        bar = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X("year:O", title="年", axis=alt.Axis(labelAngle=0)),
                y=alt.Y(
                    "year_total:Q",
                    title="年合計（円）",
                    scale=alt.Scale(domain=[0, year_upper]),
                    axis=alt.Axis(
                        values=tick_values,
                        grid=True,
                        format=",.0f",
                        labelFontSize=11,
                        labelOverlap=False,
                    ),
                ),
                tooltip=[
                    alt.Tooltip("year:O", title="年"),
                    alt.Tooltip(
                        "year_total:Q", title="年合計（円）", format=",.0f"
                    ),
                ],
            )
            .properties(height=350)
        )

        target_line = (
            alt.Chart(pd.DataFrame({"y": [TARGET]}))
            .mark_rule(color="red", strokeWidth=2)
            .encode(y="y:Q")
        )

        st.altair_chart(
            (bar + target_line).configure_axis(
                titleFontSize=13, labelFontSize=11
            ),
            width="stretch",
        )

with tab4:
    st.subheader("🧾 履歴")
    if df.empty:
        st.info("まだデータがありません．")
    else:
        if show_table:
            st.dataframe(df, width="stretch")
        else:
            st.caption("直近の記録（最新10件）")
            recent = df.sort_values("month", ascending=False).head(10)
            for _, r in recent.iterrows():
                st.write(f"**{r['month']}**  —  {int(r['salary']):,} 円")

        # 間違って取り込んだ月を消す（消したあと，明細から取り込み直せる）
        with st.expander("月のデータを削除"):
            del_month = st.selectbox(
                "削除する月",
                df.sort_values("month", ascending=False)["month"].tolist(),
                key="delete_month",
            )
            if st.button("この月を削除", key="delete_month_btn"):
                delete_month(del_month)
                st.session_state["saved_message"] = f"{del_month} を削除しました"
                st.rerun()