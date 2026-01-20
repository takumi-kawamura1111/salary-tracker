import sqlite3
from datetime import datetime, date

import pandas as pd
import streamlit as st
import altair as alt  

TARGET = 1_500_000
DB_PATH = "salaries.db"

st.set_page_config(page_title="給料トラッカー", page_icon="💰", layout="centered")
st.markdown(
    "<h1 style='text-align: center;'>給料トラッカー</h1>",
    unsafe_allow_html=True
)


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS salaries (
                month TEXT PRIMARY KEY,
                salary INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def month_str_from_date(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def upsert_month(month: str, salary: int):
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO salaries (month, salary, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                salary=excluded.salary,
                updated_at=excluded.updated_at
            """,
            (month, int(salary), now),
        )
        conn.commit()


def delete_month(month: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM salaries WHERE month = ?", (month,))
        conn.commit()


def load_data() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT month, salary, updated_at FROM salaries ORDER BY month",
            conn,
        )
    if df.empty:
        return df
    df["month"] = df["month"].astype(str)
    df["salary"] = pd.to_numeric(df["salary"], errors="coerce").fillna(0).astype(int)
    return df


def build_timeseries(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["month_date"] = pd.to_datetime(out["month"] + "-01", format="%Y-%m-%d", errors="coerce")
    out = out.dropna(subset=["month_date"]).sort_values("month_date").reset_index(drop=True)
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


# 初期化
init_db()
df = load_data()

# ===== サイドバー：表示設定 =====
st.sidebar.header("表示設定")

compact = st.sidebar.toggle("📱 コンパクト表示（スマホ推奨）", value=True)
show_table = st.sidebar.toggle("🧾 履歴テーブルを表示", value=not compact)

st.sidebar.divider()

st.sidebar.divider()
st.sidebar.caption(f"保存先：{DB_PATH}（SQLite）")

# ===== 入力（上書き） =====
with st.form("input_form", clear_on_submit=False):
    st.subheader("入力")

    # ===== 対象月選択（年＋月だけ） =====
    today = date.today()

    # 年の候補（今年±5年くらい）
    year_candidates = list(range(today.year - 5, today.year + 6))
    selected_year = st.selectbox("年", year_candidates, index=year_candidates.index(today.year))

    # 月の候補
    month_candidates = list(range(1, 13))
    selected_month = st.selectbox("月", month_candidates, index=today.month - 1)

    # YYYY-MM 文字列に変換
    month = f"{selected_year:04d}-{selected_month:02d}"

    default_salary = 0
    if not df.empty and (df["month"] == month).any():
        default_salary = int(df.loc[df["month"] == month, "salary"].iloc[0])

    salary = st.number_input("月々の給料（円）", min_value=0, step=1000, value=default_salary)
    submitted = st.form_submit_button("保存（上書き）")

if submitted:
    upsert_month(month, int(salary))
    st.success(f"{month} を保存しました（上書き）")
    df = load_data()

st.divider()

# ===== タブ（スマホ向けナビ） =====
tab1, tab2, tab3, tab4 = st.tabs(["✅ 進捗", "📅 年集計", "🧾 履歴", "📈 グラフ"])

with tab1:
    st.subheader("📅 年別進捗")

    if df.empty:
        st.info("データがありません．")
    else:
        ts = build_timeseries(df)
        years = sorted(ts["year"].unique().tolist(), reverse=True)

        # 対象年を選択
        selected_year = st.selectbox("対象年", years)

        # 年合計・進捗
        year_total = int(ts.loc[ts["year"] == selected_year, "salary"].sum())
        diff = TARGET - year_total
        progress = min(max(year_total / TARGET, 0.0), 1.0)

        st.progress(progress)

        if compact:
            st.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            st.metric(f"{selected_year}年 達成率", f"{progress * 100:.1f}%")
            if diff >= 0:
                st.metric(f"{selected_year}年 残り（円）", f"{diff:,}")
            else:
                st.metric(f"{selected_year}年 超過（円）", f"{abs(diff):,}")
        else:
            a, b, c = st.columns(3)
            a.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            if diff >= 0:
                b.metric(f"{selected_year}年 残り（円）", f"{diff:,}")
            else:
                b.metric(f"{selected_year}年 超過（円）", f"{abs(diff):,}")
            c.metric(f"{selected_year}年 達成率", f"{progress * 100:.1f}%")

        st.divider()

        # ===== 月別テーブル（未入力は空欄） =====
        st.subheader("月別")

        year_ts = ts[ts["year"] == selected_year].copy()
        # 例：ym = "2026-01" から月だけ取り出す
        year_ts["month_num"] = year_ts["month_date"].dt.month.astype(int)

        # 1〜12月を骨格として作る
        base = pd.DataFrame({"月": list(range(1, 13))})

        # 実データを月番号で合流（未入力は NaN → 空欄表示）
        merged = base.merge(
            year_ts[["month_num", "salary"]].rename(columns={"month_num": "月", "salary": "給料（円）"}),
            on="月",
            how="left"
        )

        # 表示用：数値を「,」付き文字列にして，未入力は空欄
        def fmt(x):
            if pd.isna(x):
                return ""
            return f"{int(x):,}"

        display_df = merged.copy()
        display_df["給料（円）"] = display_df["給料（円）"].apply(fmt)

        st.dataframe(display_df, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("📅 年ごとの集計")
    if df.empty:
        st.info("データを追加すると年集計が表示されます．")
    else:
        ts = build_timeseries(df)
        ys = yearly_summary(ts)

        years = sorted(ts["year"].unique().tolist(), reverse=True)
        selected_year = st.selectbox("表示する年", years)

        year_total = int(ys.loc[ys["year"] == selected_year, "year_total"].iloc[0])
        month_avg = int(ys.loc[ys["year"] == selected_year, "month_avg"].iloc[0])
        max_month_salary = int(ys.loc[ys["year"] == selected_year, "max_month_salary"].iloc[0])

        if compact:
            st.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            st.metric(f"{selected_year}年 月平均（円）", f"{month_avg:,}")
            st.metric(f"{selected_year}年 最大月給（円）", f"{max_month_salary:,}")
        else:
            a, b, c = st.columns(3)
            a.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            b.metric(f"{selected_year}年 月平均（円）", f"{month_avg:,}")
            c.metric(f"{selected_year}年 最大月給（円）", f"{max_month_salary:,}")

        st.caption("年合計（棒グラフ：10万円刻み・目標150万円）")

        chart_df = ys.sort_values("year").copy()

        # 10万円刻みの目盛り
        tick_values = list(range(0, TARGET + 1, 100_000))

        bar = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X(
                    "year:O",
                    title="年",
                    axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(
                    "year_total:Q",
                    title="年合計（円）",
                    scale=alt.Scale(domain=[0, TARGET]),
                    axis=alt.Axis(
                        values=tick_values,      # ← 10万円刻み
                        grid=True,               # ← グリッド線ON
                        format="~s"              # ← 100k, 200k 表記（日本円でも視認性◎）
                    ),
                ),
                tooltip=["year", "year_total"]
            )
            .properties(
                height=420   # ← 縦方向を広げる（スマホでも見やすい）
            )
        )

        # 目標ライン（150万円）
        target_line = (
            alt.Chart(pd.DataFrame({"y": [TARGET]}))
            .mark_rule(color="red", strokeWidth=2)
            .encode(y="y:Q")
        )

        st.altair_chart(bar + target_line, use_container_width=True)

        st.caption("年内の月別推移")
        year_ts = ts[ts["year"] == selected_year].copy()
        st.bar_chart(year_ts.set_index("month_date")[["salary"]])

with tab3:
    st.subheader("🧾 履歴")
    if df.empty:
        st.info("まだデータがありません．")
    else:
        if show_table:
            st.dataframe(df, use_container_width=True)
        else:
            st.caption("直近の記録（最新10件）")
            recent = df.sort_values("month", ascending=False).head(10)
            for _, r in recent.iterrows():
                st.write(f"**{r['month']}**  —  {int(r['salary']):,} 円")

with tab4:
    st.subheader("📈 グラフ")
    if df.empty:
        st.info("グラフはデータを追加すると表示されます．")
    else:
        ts = build_timeseries(df)

        st.caption("累計の推移")
        line_df = ts[["month_date", "cumulative"]].rename(columns={"month_date": "month"}).set_index("month")
        st.line_chart(line_df)

        st.caption("月別の給料")
        bar_df = ts[["month_date", "salary"]].rename(columns={"month_date": "month"}).set_index("month")
        st.bar_chart(bar_df)