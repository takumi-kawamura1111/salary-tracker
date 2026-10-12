from datetime import date, datetime
import os

import altair as alt
import pandas as pd
from sqlalchemy import create_engine, text
import streamlit as st

TARGET = 1_500_000

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

    # PostgreSQLの古い接頭辞 (postgres://) を postgresql:// に補正
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
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


# 初期化
init_db()
df = load_data()
auto_backup_csv(df)

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
        use_container_width=True,
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
if is_sqlite():
    st.sidebar.warning(
        "保存先：SQLite（ローカルファイル）\n\n"
        "クラウド上ではアプリの再起動・スリープでデータが消えます．"
        "Secrets に DATABASE_URL を設定してください．"
    )
else:
    st.sidebar.caption(f"保存先：{get_engine().dialect.name}")

# ===== 入力（上書き） =====
with st.form("input_form", clear_on_submit=False):
    st.subheader("入力")

    today = date.today()
    year_candidates = list(range(today.year - 5, today.year + 6))
    selected_year = st.selectbox(
        "年", year_candidates, index=year_candidates.index(today.year)
    )

    month_candidates = list(range(1, 13))
    selected_month = st.selectbox(
        "月", month_candidates, index=today.month - 1
    )

    month = f"{selected_year:04d}-{selected_month:02d}"

    default_salary = 0
    if not df.empty and (df["month"] == month).any():
        default_salary = int(
            df.loc[df["month"] == month, "salary"].iloc[0]
        )

    salary = st.number_input(
        "月々の給料（円）", min_value=0, step=1000, value=default_salary
    )
    submitted = st.form_submit_button("保存（上書き）")

if submitted:
    upsert_month(month, int(salary))
    st.success(f"{month} を保存しました（上書き）")
    df = load_data()

st.divider()

# ===== タブ =====
tab1, tab2, tab3, tab4 = st.tabs(
    ["✅ 進捗", "📅 年集計", "📈 グラフ", "🧾 履歴"]
)

with tab1:
    st.subheader("📅 年別進捗")

    if df.empty:
        st.info("データがありません．")
    else:
        ts = build_timeseries(df)
        years = sorted(ts["year"].unique().tolist(), reverse=True)
        selected_year = st.selectbox("対象年", years)

        year_total = int(
            ts.loc[ts["year"] == selected_year, "salary"].sum()
        )
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

        st.subheader("月別")
        year_ts = ts[ts["year"] == selected_year].copy()
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

        year_total = int(
            ys.loc[ys["year"] == selected_year, "year_total"].iloc[0]
        )
        month_avg = int(
            ys.loc[ys["year"] == selected_year, "month_avg"].iloc[0]
        )
        max_month_salary = int(
            ys.loc[ys["year"] == selected_year, "max_month_salary"].iloc[0]
        )

        if compact:
            st.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            st.metric(f"{selected_year}年 月平均（円）", f"{month_avg:,}")
            st.metric(
                f"{selected_year}年 最大月給（円）", f"{max_month_salary:,}"
            )
        else:
            a, b, c = st.columns(3)
            a.metric(f"{selected_year}年 合計（円）", f"{year_total:,}")
            b.metric(f"{selected_year}年 月平均（円）", f"{month_avg:,}")
            c.metric(
                f"{selected_year}年 最大月給（円）", f"{max_month_salary:,}"
            )

with tab3:
    if df.empty:
        st.info("データがありません．")
    else:
        ts = build_timeseries(df)
        ys = yearly_summary(ts)

        years = sorted(ts["year"].unique().tolist(), reverse=True)
        selected_year_graph = st.selectbox(
            "対象年（グラフ）", years, key="graph_year"
        )

        st.markdown(
            "<h3 style='margin-top: 20px; margin-bottom: 4px;'>年内の月別推移</h3>",
            unsafe_allow_html=True,
        )

        year_ts = ts[ts["year"] == selected_year_graph].copy()
        year_ts = year_ts.sort_values("month_date").reset_index(drop=True)
        year_ts["month_label"] = year_ts["month_date"].dt.strftime("%m")

        month_target = 200_000
        month_tick_values = list(range(0, month_target + 1, 20_000))

        month_bar = (
            alt.Chart(year_ts)
            .mark_bar()
            .encode(
                x=alt.X(
                    "month_label:O", title="月", axis=alt.Axis(labelAngle=0)
                ),
                y=alt.Y(
                    "salary:Q",
                    title="給料（円）",
                    scale=alt.Scale(domain=[0, month_target]),
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
            .properties(height=520)
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
            use_container_width=True,
        )

        st.markdown(
            "<h3 style='margin-top: 20px; margin-bottom: 4px;'>年ごとの推移</h3>",
            unsafe_allow_html=True,
        )

        chart_df = ys.sort_values("year").copy()
        tick_values = list(range(0, TARGET + 1, 100_000))

        bar = (
            alt.Chart(chart_df)
            .mark_bar()
            .encode(
                x=alt.X("year:O", title="年", axis=alt.Axis(labelAngle=0)),
                y=alt.Y(
                    "year_total:Q",
                    title="年合計（円）",
                    scale=alt.Scale(domain=[0, TARGET]),
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
            .properties(height=690)
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
            use_container_width=True,
        )

with tab4:
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