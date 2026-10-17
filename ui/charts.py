"""Plotly charts for the dashboard, carry-forward and GSTR-3B pages.

Colours follow the entity everywhere: Books = blue, portal (2B/1) = orange,
GSTR-3B = aqua; single-series charts use one hue.  Every chart has hover tooltips.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from config import CHART_3B_COLOR, CHART_BOOKS_COLOR, CHART_GRID_COLOR, CHART_PORTAL_COLOR, CHART_SINGLE_COLOR
from modules.formatting import format_inr
from modules.matcher import ReconResult, status_table, supplier_summary


def style_fig(fig: go.Figure, height: int = 340, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=40, b=8), barcornerradius=4, bargap=0.35, bargroupgap=0.08,
        showlegend=legend, legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        hoverlabel=dict(namelength=-1), font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif"),
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=CHART_GRID_COLOR, zeroline=False)
    return fig


def _bar(name: str, x, y, color: str) -> go.Bar:
    return go.Bar(name=name, x=x, y=y, marker_color=color, customdata=pd.Series(y).map(format_inr),
                  hovertemplate=f"<b>%{{x}}</b><br>{name}: %{{customdata}}<extra></extra>")


def _month_labels(months: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(months, format="%Y-%m", errors="coerce")
    return parsed.dt.strftime("%b %Y").where(parsed.notna(), months)


def status_counts(recon: pd.DataFrame) -> go.Figure:
    t = status_table(recon)
    fig = go.Figure(go.Bar(
        y=t["status"], x=t["documents"], orientation="h", marker_color=CHART_SINGLE_COLOR,
        text=t["documents"], textposition="outside", cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x:,} documents<extra></extra>"))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(gridcolor=CHART_GRID_COLOR, showgrid=True, title="Documents")
    return style_fig(fig, legend=False).update_layout(title="Documents by status")


def itc_by_status(recon: pd.DataFrame, short: str, tw: str = "ITC") -> go.Figure:
    t = status_table(recon)
    fig = go.Figure([_bar("Books", t["status"], t["itc_books"], CHART_BOOKS_COLOR),
                     _bar(short, t["status"], t["itc_portal"], CHART_PORTAL_COLOR)])
    fig.update_yaxes(title=f"Net {tw} (₹)", tickformat="~s")
    return style_fig(fig).update_layout(title=f"Net {tw} by status - Books vs {short}", barmode="group")


def itc_by_month(recon: pd.DataFrame, short: str, tw: str = "ITC") -> go.Figure:
    t = recon[recon["month"] != ""].groupby("month")[["itc_books", "itc_portal"]].sum().reset_index()
    labels = _month_labels(t["month"])
    fig = go.Figure([_bar("Books", labels, t["itc_books"], CHART_BOOKS_COLOR),
                     _bar(short, labels, t["itc_portal"], CHART_PORTAL_COLOR)])
    fig.update_yaxes(title=f"Net {tw} (₹)", tickformat="~s")
    return style_fig(fig).update_layout(title=f"Net {tw} by invoice month", barmode="group")


def top_pending(res: ReconResult, recon: pd.DataFrame) -> go.Figure | None:
    sup = supplier_summary(recon, res.portal_name)
    sup = sup[sup["pending_itc"] > 0].head(10)
    if sup.empty:
        return None
    labels = sup["party_name"].where(sup["party_name"] != "", sup["gstin"]).str.slice(0, 32)
    fig = go.Figure(go.Bar(
        y=labels, x=sup["pending_itc"], orientation="h", marker_color=CHART_SINGLE_COLOR,
        text=sup["pending_itc"].map(lambda v: format_inr(v, decimals=0)), textposition="outside", cliponaxis=False,
        customdata=sup[["gstin"]].assign(v=sup["pending_itc"].map(format_inr)).to_numpy(),
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}<br>Pending ITC: %{customdata[1]}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=CHART_GRID_COLOR, tickformat="~s", title="Pending ITC (₹)")
    return style_fig(fig, legend=False).update_layout(title="Top suppliers by ITC at risk")


def carry_forward(schedule: pd.DataFrame, short: str) -> go.Figure:
    labels = _month_labels(schedule["month"])
    fig = go.Figure([_bar("New in books", labels, schedule["added_itc"], CHART_BOOKS_COLOR),
                     _bar(f"Reflected in {short}", labels, schedule["reflected_itc"], CHART_PORTAL_COLOR)])
    fig.update_yaxes(title="Net ITC (₹)", tickformat="~s")
    return style_fig(fig).update_layout(title=f"ITC booked vs reflected in {short}, by month", barmode="group")


def closing_pending(schedule: pd.DataFrame) -> go.Figure:
    labels = _month_labels(schedule["month"])
    fig = go.Figure(go.Bar(
        x=labels, y=schedule["closing_itc"], marker_color=CHART_SINGLE_COLOR,
        customdata=schedule[["closing_count"]].assign(v=schedule["closing_itc"].map(format_inr)).to_numpy(),
        hovertemplate="<b>%{x}</b><br>%{customdata[0]} documents<br>Pending ITC: %{customdata[1]}<extra></extra>"))
    fig.update_yaxes(title="Pending ITC (₹)", tickformat="~s")
    return style_fig(fig, legend=False).update_layout(title="ITC still pending at month end (carried forward)")


def gstr3b_months(comparison: pd.DataFrame) -> go.Figure:
    t = comparison[comparison["month"] != "TOTAL"]
    labels = _month_labels(t["month"])
    fig = go.Figure([_bar("Books", labels, t["books_total"], CHART_BOOKS_COLOR),
                     _bar("2B (available)", labels, t["gstr2b_total"], CHART_PORTAL_COLOR),
                     _bar("3B claimed 4A(5)", labels, t["gstr3b_total"], CHART_3B_COLOR)])
    fig.update_yaxes(title="ITC (₹)", tickformat="~s")
    return style_fig(fig).update_layout(title="ITC by month - Books vs GSTR-2B vs GSTR-3B", barmode="group")
