#!/usr/bin/env python3
"""Create an offline HTML report, CSV lists and figures from batch-result.json."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path


FLAGS = {
    "unpaired_jerk_candidate":"单侧 jerk 候选（未成对）",
    "missing_jerk_contact":"该轮 jerk 触地缺失",
    "ambiguous_front_pair":"前轮候选配对有歧义",
    "nearby_cycle_assigned_elsewhere":"附近预测周期已分配给其他候选",
    "match_far_from_cycle_zero":"匹配触地远离预测周期归零",
    "absolute_error_gt_500mm":"绝对误差超过 500 mm",
    "non_bump_feature_at_contact":"触地处输出类别不是减速带",
    "no_true_zero_in_recording":"录波内没有真零样本",
    "interpolation_gap_gt_50ms":"触地插值跨度超过 50 ms",
}
STATUS = {"late":"偏晚","early":"偏早","missed":"预测未匹配",
          "contact_unavailable":"触地缺失","outside_prediction_coverage":"超出预测覆盖",
          "invalid_calculation":"计算不可用"}
LABELS = {
    "sample_id":"样本ID","scene_id":"场景编号","scene":"场景名","event":"候选事件编号","wheel":"车轮",
    "contact_available":"jerk触地可用","paired_front_contacts":"双轮触地成对","pair_ambiguous":"双轮配对有歧义",
    "pair_peak_gap_ms":"双轮峰时差_ms","gate_eligible":"双轮门禁通过","startdist_fl":"FL_StartDist_0","startdist_fr":"FR_StartDist_0",
    "contact_time_s":"相对触地时间_s","contact_timestamp_ns":"触地epoch_ns（文本）","speed_kph":"触地车速_kph",
    "jerk_peak_mps3":"jerk峰值_mps3","jerk_peak_ratio":"jerk峰值阈值比","jerk_oscillation_peaks":"振荡峰数",
    "feature_at_contact":"触地处道路类别","distance_at_contact_mm":"触地处距离插值_mm","previous_hold_distance_mm":"触地处距离前值_mm",
    "control_bracket_ms":"距离插值跨度_ms","left_timestamp_ns":"左端epoch_ns（文本）","right_timestamp_ns":"右端epoch_ns（文本）",
    "left_distance_mm":"左端距离_mm","right_distance_mm":"右端距离_mm","detection_cycle_id":"预测周期ID",
    "detection_time_s":"预测周期起点_s","zero_time_s":"预测真零时间_s","predicted_zero_timestamp_ns":"预测真零epoch_ns（文本）",
    "time_error_ms":"真零减触地时间_ms","status":"状态","error_mm":"带符号误差_mm","raw_core_error_mm":"核心原始误差_mm",
    "independent_error_mm":"独立复算误差_mm","flags":"质量标记","related_samples":"关联样本ID",
    "used_for_accuracy":"纳入精度统计","exclusion_reason":"不纳入原因","state":"计算状态","group_count":"jerk候选组数",
    "expected":"补齐双轮候选槽位","contact_missing":"触地缺失槽位","gate_failed":"门禁失败轮次","gate_unknown":"门禁未知轮次",
    "gate_eligible":"门禁合格轮次","detected":"门禁内检出轮次","out_of_range":"门禁内超范围轮次",
    "accuracy_samples":"有效精度样本数","detection_rate":"门禁内检出率","p90_abs_mm":"绝对误差P90_mm","p95_abs_mm":"绝对误差P95_mm",
    "signed_mean_mm":"带符号均值_mm","sample_std_mm":"样本标准差_mm","sample_variance_mm2":"样本方差_mm2",
    "early":"精度样本偏早数","late":"精度样本偏晚数","unmatched_predictions_all":"全部未匹配轮次",
    "unmatched_predictions_gated":"门禁内未匹配轮次","raw_detected_all":"全部可计算轮次","raw_out_of_range_all":"全部超范围轮次",
    "flagged_rows":"含质量标记行数","FL_p95_abs_mm":"FL_P95_mm","FR_p95_abs_mm":"FR_P95_mm",
    "kind":"数据质量类型","timestamp_ns":"时间epoch_ns（文本）","source_line":"源CSV行号","reason":"原因",
    "cycle_id":"预测周期ID","detection_distance_mm":"预测周期起点距离_mm","coarse_zero_time_s":"低于100mm粗完成时间_s",
    "assigned_sample":"分配的样本ID",
}


def fmt(value, decimals=2):
    if value is None:
        return "—"
    if isinstance(value,bool):
        return "是" if value else "否"
    if isinstance(value,float):
        return f"{value:.{decimals}f}"
    return html.escape(str(value))


def write_csv(path, rows, fields=None):
    fields = fields or (list(rows[0]) if rows else ["note"])
    with path.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.writer(stream)
        writer.writerow([("双轮门禁通过" if k=="gate_eligible" and "sample_id" in fields else "实际jerk触地轮次" if k=="contact_available" and "expected" in fields else LABELS.get(k,k)) for k in fields])
        for r in rows:
            values=[]
            for key in fields:
                value=r.get(key)
                if key=="flags":value="；".join(FLAGS.get(f,f) for f in value or [])
                elif key=="status":value=STATUS.get(value,value)
                elif isinstance(value,list):value="；".join(value)
                elif isinstance(value,bool):value="是" if value else "否"
                elif key.endswith("timestamp_ns") or key=="timestamp_ns":
                    # CSV has no type schema; apostrophe prevents Excel losing
                    # precision in 19-digit timestamps. JSON retains integers.
                    value="'"+str(value) if value is not None else None
                values.append(value)
            writer.writerow(values)


def make_figures(report, source, output, plot_deps):
    sys.path.insert(0,str(plot_deps.resolve()))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    import numpy as np
    font=Path("C:/Windows/Fonts/msyh.ttc")
    family=FontProperties(fname=str(font)).get_name() if font.exists() else "DejaVu Sans"
    figure_dir=output/"figures"
    figure_dir.mkdir()
    with plt.rc_context({"font.family":family,"font.size":9,"axes.unicode_minus":False,
                         "axes.spines.top":False,"axes.spines.right":False,
                         "figure.facecolor":"white","savefig.facecolor":"white"}):
        scenes=report["scene_summary"]
        fig,axes=plt.subplots(1,2,figsize=(14,9),layout="constrained",gridspec_kw={"width_ratios":[1.4,1]})
        categories=[("触地缺失","contact_missing","#C6CBD2","//"),("门禁失败","gate_failed","#DDAA33",""),
                    ("门禁内未匹配","unmatched_predictions_gated","#AA3377",""),("门禁内超范围","out_of_range","#882255","xx"),
                    ("有效精度样本","accuracy_samples","#4477AA","")]
        y=np.arange(len(scenes));left=np.zeros(len(scenes))
        for label,key,color,hatch in categories:
            values=np.array([s[key] for s in scenes]);axes[0].barh(y,values,left=left,color=color,hatch=hatch,label=label,height=.7);left+=values
        labels=[s["scene_id"]+"  "+s["scene"][11:19].replace("-",":") for s in scenes]
        axes[0].set(yticks=y,yticklabels=labels,xlabel="候选双轮槽位数（包含未检出 jerk 的补齐行）",title="样本覆盖与排除原因")
        axes[0].invert_yaxis();axes[0].legend(loc="lower right",fontsize=8);axes[0].grid(axis="x",alpha=.15)
        for i,s in enumerate(scenes):
            if s["p95_abs_mm"] is None:
                axes[1].text(5,i,"不可用 · n=0",va="center",color="#777777",fontsize=8)
            else:
                axes[1].plot(s["p95_abs_mm"],i,"o",color="#225588",ms=6)
                axes[1].text(s["p95_abs_mm"]+4,i,f"{s['p95_abs_mm']:.1f} · n={s['accuracy_samples']}",va="center",fontsize=8)
        axes[1].set(yticks=y,yticklabels=labels,xlabel="绝对误差 P95（mm）",title="各场景有效样本的 P95",xlim=(0,max((s["p95_abs_mm"] or 0) for s in scenes)*1.2+15))
        axes[1].set_ylim(len(scenes)-.5,-.5);axes[1].grid(axis="x",alpha=.15)
        fig.suptitle(f"{len(scenes)} 场景到点精度｜自动 jerk 候选口径\n整体 P95 {report['overall']['combined']['p95_abs_mm']:.2f} mm · 有效 {report['overall']['combined']['accuracy_samples']} 轮次 · {sum(s['accuracy_samples']==0 for s in scenes)} 场景无有效样本",fontsize=14,weight="bold")
        fig.savefig(figure_dir/"scene-summary.png",dpi=160)
        fig.savefig(figure_dir/"scene-summary.svg")
        plt.close(fig)

        used=[r for r in report["detail"] if r["used_for_accuracy"]]
        bins=np.arange(np.floor(min(r["error_mm"] for r in used)/10)*10,np.ceil(max(r["error_mm"] for r in used)/10)*10+10,10)
        fig,axes=plt.subplots(2,1,figsize=(12,6),layout="constrained",gridspec_kw={"height_ratios":[2,1]})
        axes[0].hist([[r["error_mm"] for r in used if r["wheel"]==w] for w in ("FL","FR")],bins=bins,stacked=True,
                     color=["#4477AA","#EE7733"],label=[f"{w}（{sum(r['wheel']==w for r in used)} 轮次）" for w in ("FL","FR")],edgecolor="white")
        axes[0].set(ylabel="轮次数",title="有效精度样本带符号误差分布 · 10 mm 分箱（超范围样本另列）")
        axes[0].legend();axes[0].axvline(0,color="#111111",lw=.8);axes[0].grid(axis="y",alpha=.15)
        for yi,(w,c,marker) in enumerate((("FL","#4477AA","o"),("FR","#CC6622","s"))):
            vals=[r["error_mm"] for r in used if r["wheel"]==w]
            axes[1].scatter(vals,[yi]*len(vals),color=c,marker=marker,s=40,label=w)
        axes[1].set(yticks=[0,1],yticklabels=["FL","FR"],xlabel="带符号距离误差（mm）：负值偏早，正值偏晚",ylim=(-.5,1.5))
        axes[1].axvline(0,color="#111111",lw=.8);axes[1].grid(axis="x",alpha=.15)
        for ax in axes:ax.set_xlim(bins[0]-10,bins[-1]+10)
        fig.savefig(figure_dir/"signed-distribution.png",dpi=160)
        fig.savefig(figure_dir/"signed-distribution.svg")
        plt.close(fig)

        for s in report["scenes"]:
            if s["state"]!="CALCULATED":continue
            data=json.loads((source/f"{s['scene_id']}-waveforms.json").read_text(encoding="utf-8"))
            t=np.array(data["control_time_s"])
            fig,axes=plt.subplots(3,2,figsize=(14,9),layout="constrained")
            for col,(w,color) in enumerate((("fl","#0072B2"),("fr","#D55E00"))):
                wf=data["wheels"][w];tj=np.array(wf["jerk_time_s"]);j=np.array(wf["jerk_mps3"]);th=np.array(wf["threshold_mps3"])
                axes[0,col].plot(tj,j,color=color,lw=.65);axes[0,col].plot(tj,th,"--",color="#666666",lw=.7);axes[0,col].plot(tj,-th,"--",color="#666666",lw=.7)
                axes[0,col].set(title=w.upper()+" · 原始 jerk 与速度阈值",ylabel="jerk（m/s³）")
                axes[1,col].plot(t,wf["distance_mm"],color=color,lw=.85)
                axes[1,col].set(title="PCM 外发距离 · 同时刻最后成功消息",ylabel="距离（mm）")
                axes[2,col].step(t,data["startdist_fl"],where="post",color="#0072B2",lw=1.8,label="FL StartDist[0]")
                axes[2,col].step(t,data["startdist_fr"],where="post",color="#D55E00",ls="--",lw=1.2,label="FR StartDist[0]")
                axes[2,col].set(ylabel="StartDist",xlabel="原始录波相对时间（s）",ylim=(-.1,1.3),yticks=[0,1]);axes[2,col].legend(fontsize=7,loc="lower left")
                for r in data["detail"]:
                    if r["wheel"]!=w.upper() or not r["contact_available"]:continue
                    c="#228833" if r["gate_eligible"] else "#777777"
                    for row in range(3):axes[row,col].axvline(r["contact_time_s"],color=c,ls="--",lw=.8,alpha=.75)
                    axes[0,col].text(r["contact_time_s"],.96,f"e{r['event']}",transform=axes[0,col].get_xaxis_transform(),rotation=90,va="top",ha="right",fontsize=7)
                    if r["distance_at_contact_mm"] is not None:axes[1,col].plot(r["contact_time_s"],r["distance_at_contact_mm"],"o",color=c,ms=4)
                for row in range(3):axes[row,col].set_xlim(0,max(float(tj[-1]),float(t[-1])));axes[row,col].grid(alpha=.12)
            m=s["metrics"]["combined"]
            fig.suptitle(f"{s['scene_id']}  {s['scene']}\n有效 n={m['accuracy_samples']} · P95={fmt(m['p95_abs_mm'])} mm · 绿线=门禁通过，灰线=门禁失败；e 为自动候选组",fontsize=11,weight="bold")
            fig.savefig(figure_dir/f"{s['scene_id']}.png",dpi=130)
            plt.close(fig)
    return {"matplotlib":matplotlib.__version__,"numpy":np.__version__,"font":family,
            "transformations":["raw jerk, no extra smoothing","distance at contact uses core linear interpolation",
                               "StartDist previous hold","distribution uses only gated finite abs(error)<=500mm; other rows retained in separate lists"],
            "signed_distribution_bin_width_mm":10}


def detail_table(rows, table_id):
    columns=[("sample_id","样本ID"),("contact_time_s","触地 s"),("speed_kph","车速 kph"),("gate_eligible","门禁"),
             ("status","状态"),("error_mm","误差 mm"),("used_for_accuracy","计入精度"),("flags","质量标记"),("related_samples","关联样本")]
    body=[]
    for r in rows:
        cells=[]
        for key,_ in columns:
            value=r.get(key)
            if key=="sample_id":cell=f'<a href="#plot-{r["scene_id"]}">{html.escape(str(value))}</a>'
            elif key=="status":cell=html.escape(STATUS.get(value,value))
            elif key=="flags":cell=html.escape("；".join(FLAGS.get(f,f) for f in value)) or "—"
            elif isinstance(value,list):cell=html.escape("；".join(value)) or "—"
            else:cell=fmt(value,3 if key=="contact_time_s" else 2)
            cells.append("<td>"+cell+"</td>")
        body.append(f'<tr data-gated="{str(r["gate_eligible"]).lower()}">'+"".join(cells)+"</tr>")
    return '<div class="scroll"><table class="filterable" id="'+table_id+'"><thead><tr>'+"".join("<th>"+label+"</th>" for _,label in columns)+"</tr></thead><tbody>"+"".join(body)+"</tbody></table></div>"


def build_report(result_path: Path, output: Path, plot_deps: Path):
    if output.exists():raise FileExistsError("choose a new delivery directory")
    report=json.loads(result_path.read_text(encoding="utf-8"));source=result_path.parent
    output.mkdir(parents=True,exist_ok=False)
    rows=report["detail"];m=report["overall"]["combined"]
    fields=list(rows[0])
    subsets={"全部到点明细":rows,"预测未匹配清单":[r for r in rows if r["status"]=="missed"],
             "门禁不通过清单":[r for r in rows if r["gate_eligible"] is False],
             "触地缺失清单":[r for r in rows if not r["contact_available"]],
             "超范围样本清单":[r for r in rows if r["error_mm"] is not None and abs(r["error_mm"])>500],
             "异常样本清单":[r for r in rows if r["flags"]]}
    for name,subset in subsets.items():write_csv(output/(name+".csv"),subset,fields)
    write_csv(output/"各场景汇总.csv",report["scene_summary"])
    write_csv(output/"整体及分轮汇总.csv",[{"scope":k,**v} for k,v in report["overall"].items()])
    write_csv(output/"速度分组汇总.csv",[{"speed_bin":k,**v} for k,v in report["speed_bins"].items()])
    quality=[d for s in report["scenes"] for d in s["data_quality"]]
    write_csv(output/"原始数据质量清单.csv",quality)
    cycles=[d for s in report["scenes"] for d in s["prediction_cycles"]]
    write_csv(output/"预测周期清单.csv",cycles)
    shutil.copy2(result_path,output/"batch-result.json")
    figure_meta=make_figures(report,source,output,plot_deps)
    used_flagged=sum(r["used_for_accuracy"] and bool(r["flags"]) for r in rows)
    contributing=sum(s.get("accuracy_samples",0)>0 for s in report["scene_summary"])
    scene_rows=[]
    for s in report["scene_summary"]:
        cols=[f'<a href="#plot-{s["scene_id"]}">{s["scene_id"]}</a>',html.escape(s["scene"][11:19].replace("-",":")),
              s["group_count"],s["contact_available"],s["gate_eligible"],s["gate_failed"],s["contact_missing"],
              s["unmatched_predictions_gated"],s["out_of_range"],s["accuracy_samples"],fmt(s["FL_p95_abs_mm"]),fmt(s["FR_p95_abs_mm"]),fmt(s["p95_abs_mm"]),fmt(s["signed_mean_mm"])]
        scene_rows.append('<tr>'+"".join("<td>"+str(c)+"</td>" for c in cols)+"</tr>")
    overall_rows="".join("<tr>"+"".join("<td>"+str(v)+"</td>" for v in [name.upper() if name!="combined" else "合并",x["accuracy_samples"],fmt(x["p90_abs_mm"]),fmt(x["p95_abs_mm"]),fmt(x["signed_mean_mm"]),fmt(x["sample_std_mm"]),fmt(x["sample_variance_mm2"]),x["early"],x["late"]])+"</tr>" for name,x in report["overall"].items())
    speed_rows="".join("<tr>"+"".join("<td>"+str(v)+"</td>" for v in [name.replace("_kph",""),x["contact_available"],x["gate_eligible"],x["accuracy_samples"],fmt(x["p90_abs_mm"]),fmt(x["p95_abs_mm"]),fmt(x["signed_mean_mm"]),fmt(x["sample_std_mm"])])+"</tr>" for name,x in report["speed_bins"].items())
    flags_rows="".join(f"<tr><td>{html.escape(FLAGS.get(k,k))}</td><td>{n}</td></tr>" for k,n in report["flag_counts"].items())
    gated_misses=[r for r in rows if r["gate_eligible"] is True and r["status"]=="missed"]
    missed_section=detail_table(gated_misses,"gated-misses")
    all_sections=[]
    for key,title in [("预测未匹配清单",f"全部预测未匹配：{m['unmatched_predictions_all']} 条，其中门禁内 {m['unmatched_predictions_gated']} 条、门禁外 {m['unmatched_predictions_all']-m['unmatched_predictions_gated']} 条"),
                      ("门禁不通过清单",f"门禁不通过：{m['gate_failed']} 条"),("超范围样本清单",f"超范围：{m['raw_out_of_range_all']} 条，其中门禁内 {m['out_of_range']} 条"),
                      ("触地缺失清单",f"触地缺失：{m['contact_missing']} 条补齐槽位，不计为预测漏检"),("异常样本清单",f"含质量标记：{m['flagged_rows']} 行，包含 {m['contact_missing']} 条触地缺失补齐行")]:
        all_sections.append(f'<details><summary>{title} <a class="download" href="{key}.csv">下载 CSV</a></summary>{detail_table(subsets[key],"table-"+key)}</details>')
    plot_sections="".join(f'<details id="plot-{s["scene_id"]}"><summary>{s["scene_id"]} · {html.escape(s["scene"])} · 有效样本 {s["metrics"]["combined"]["accuracy_samples"]}</summary><img loading="lazy" src="figures/{s["scene_id"]}.png" alt="{s["scene_id"]} 的原始 jerk、外发距离与双轮 StartDist，标出所有自动候选触地"><p><a href="figures/{s["scene_id"]}.png">打开原图</a></p></details>' for s in report["scenes"] if s["state"]=="CALCULATED")
    downloads="".join(f'<a href="{html.escape(p.name)}">{html.escape(p.name)}</a>' for p in sorted(output.glob("*.csv")))
    sens=report["paired_unambiguous_sensitivity"]
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCENE_COUNT 场景到点精度汇总</title>
<style>body{margin:0;background:#f4f6fa;color:#17283e;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1400px;margin:auto;padding:36px 26px}h1{font-size:32px;margin:0}h2{font-size:22px;margin:32px 0 12px}h3{margin:20px 0 8px}p{margin:8px 0}.muted{color:#607085}.tag{display:inline-block;padding:3px 10px;border-radius:14px;background:#e2eaf4;font-size:12px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.card,section{background:white;border:1px solid #e0e5ec;border-radius:12px;padding:20px;margin-bottom:18px}.number{font-size:30px;font-weight:700;color:#1d4f82}.warning{padding:16px;border-left:5px solid #c58728;background:#fff5df}.flow{font:15px/1.9 monospace;background:#edf3fa;padding:14px;border-radius:8px;white-space:pre-wrap}.scroll{overflow:auto;max-height:650px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #e2e6ec;padding:8px 10px;text-align:right;vertical-align:top}th{background:#eef3f8;position:sticky;top:0;white-space:nowrap}td:first-child,th:first-child{text-align:left}tr:nth-child(even){background:#fafbfd}a{color:#1d5c98}img{max-width:100%;height:auto;border-radius:6px}details{padding:12px 0;border-bottom:1px solid #dce2eb}summary{cursor:pointer;font-weight:600}.download{font-weight:400;font-size:12px;margin-left:14px}.downloads{display:flex;gap:12px;flex-wrap:wrap}input,select{padding:8px 10px;border:1px solid #b9c6d6;border-radius:6px;font:inherit}code{font-size:12px;overflow-wrap:anywhere}.small{font-size:12px}.filterbar{display:flex;gap:12px;margin:14px 0;align-items:center}@media(max-width:800px){.cards{grid-template-columns:1fr 1fr}main{padding:20px 12px}h1{font-size:26px}}</style>
<main><span class="tag">2026-10-20 · 本地离线报告 · 自动 jerk 候选口径</span><h1>SCENE_COUNT 场景到点精度</h1><p class="muted">RUN_ID</p>
<div class="cards"><div class="card">整体绝对误差 P95<div class="number">POOLED_P95 <small>mm</small></div></div><div class="card">有效精度样本<div class="number">VALID_COUNT <small>轮次</small></div>来自 CONTRIBUTING_COUNT / SCENE_COUNT 场景</div><div class="card">门禁内匹配<div class="number">DETECTED_COUNT / GATE_COUNT</div>DETECTION_RATE，包含 GATED_OUT_COUNT 条超范围</div><div class="card">门禁失败<div class="number">GATE_FAILED_COUNT <small>轮次</small></div>其余 MISSING_COUNT 槽位触地缺失、门禁未知</div></div>
<div class="warning"><strong>这份汇总保留自动检测与原脚本匹配结果。</strong> jerk 候选尚未人工确认为减速带；GATED_MISS_COUNT 条门禁内未匹配记录均存在“附近预测周期已被较早候选占用”。它们不能直接认定为模型漏检。NO_VALID_SCENE_COUNT 个场景无有效精度样本，均显示不可用。主统计 VALID_COUNT 条中有 USED_FLAGGED 条带前轴配对质量标记，按约定保留；相关异常单独列出。</div>
<h2>样本口径与分母</h2><div class="flow">GROUP_COUNT 个自动 jerk 候选组 × 2 前轮 = EXPECTED_COUNT 候选槽位（不是人工真值事件数）
EXPECTED_COUNT = CONTACT_COUNT 个实际 jerk 触地候选 + MISSING_COUNT 个对侧触地缺失槽位
CONTACT_COUNT = GATE_FAILED_COUNT 个门禁失败 + GATE_COUNT 个门禁合格
GATE_COUNT = GATED_MISS_COUNT 个预测未匹配 + DETECTED_COUNT 个已匹配
DETECTED_COUNT = GATED_OUT_COUNT 个 |误差| &gt; 500 mm + VALID_COUNT 个精度样本</div><p>漏检、门禁失败、超范围和质量标记清单可能重叠，不能把清单行数直接相加。缺少 jerk 触地的补齐行单列，既不算预测漏检，也不算门禁失败。</p>
<section><h2>整体与分轮精度</h2><p>正误差表示偏晚，负误差表示偏早。分位数在全部有效轮次上合并计算，没有平均各场景 P95。</p><div class="scroll"><table><thead><tr><th>范围</th><th>样本数</th><th>P90 mm</th><th>P95 mm</th><th>带符号均值 mm</th><th>样本标准差 mm</th><th>样本方差 mm²</th><th>偏早</th><th>偏晚</th></tr></thead><tbody>OVERALL_ROWS</tbody></table></div><p class="small">主统计仅使用门禁通过、误差有限且 |误差| ≤ 500 mm 的行；样本方差与标准差 ddof=1。没有自行设置验收阈值。</p></section>
<section><h2>各场景汇总</h2><p class="muted">点击场景编号跳到完整波形。— 表示无可用精度样本，不表示误差为零。</p><div class="scroll"><table><thead><tr><th>场景</th><th>时间</th><th>候选组</th><th>jerk轮次</th><th>门禁合格</th><th>门禁失败</th><th>触地缺失</th><th>门禁内未匹配</th><th>门禁内超范围</th><th>精度n</th><th>FL P95</th><th>FR P95</th><th>合并P95 mm</th><th>均值mm</th></tr></thead><tbody>SCENE_ROWS</tbody></table></div><img src="figures/scene-summary.png" alt="18 场景覆盖分解和各场景 P95；无有效样本的场景标为不可用"></section>
<section><h2>速度分组与带符号分布</h2><table><thead><tr><th>速度 kph</th><th>jerk轮次</th><th>门禁合格</th><th>精度n</th><th>P90 mm</th><th>P95 mm</th><th>均值mm</th><th>样本标准差mm</th></tr></thead><tbody>SPEED_ROWS</tbody></table><img src="figures/signed-distribution.png" alt="VALID_COUNT 个精度样本的 10 mm 分箱带符号误差分布与逐轮观测点；超范围样本另列"></section>
<section><h2>优先核验：GATED_MISS_COUNT 条门禁内预测未匹配</h2><p>每条均给出其附近预测周期被分配到的关联样本。此处保留原脚本的全候选贪心匹配，不为改善指标而回收或重配周期。</p>GATED_MISSES<h3>质量标记计数</h3><table><thead><tr><th>标记</th><th>行数</th></tr></thead><tbody>FLAGS_ROWS</tbody></table><p class="small">标记可重叠，且不自动排除主统计。只保留“双轮触地成对且无多候选配对歧义”的敏感性口径为 n=SENS_N，P95=SENS_P95 mm，仅作为配对影响诊断，不替换主结果。</p></section>
<section><h2>单独清单</h2><div class="filterbar"><input id="search" placeholder="按场景、样本ID或标记筛选" aria-label="筛选清单"><select id="gateFilter" aria-label="按门禁筛选"><option value="all">全部门禁状态</option><option value="true">仅门禁通过</option><option value="false">仅门禁失败</option><option value="none">仅门禁未知</option></select><span id="shown" class="muted"></span></div>ALL_SECTIONS<p>另有 QUALITY_COUNT 条原始数据质量记录：TAIL_COUNT 条不完整 CSV 尾行、RESET_COUNT 次大距离跳零。详见 <a href="原始数据质量清单.csv">原始数据质量清单</a>。DUPLICATE_GROUP_COUNT 组重复时间戳冲突已按最后成功整条消息处理，未被当作多个统计样本。</p></section>
<section><h2>SCENE_COUNT 场景完整波形</h2><p>所有自动 jerk 候选都画在原始录波时间轴上；未对 jerk 额外平滑。距离按控制时间去重，图中的圆点是候选触地处距离读数。单侧缺失不会合成触地时刻。</p>PLOTS</section>
<section><h2>计算与证据</h2><ol><li>使用 <code>payload_ctrl_ts_ms</code> 与 <code>fl/fr_dist_out_mm_0</code>。每个控制时间取 <code>dds_write_ok=1</code> 且序号最大的整条消息，保留同消息车速、类别与 StartDist。</li><li>按已找到核心的原规则，用 jerk 和车速独立产生触地候选；预测距离/类别不参与真值修正。核心的 1 秒前轴峰时差窗口只用于候选分组。</li><li>每轮在全部 jerk 候选上调用原 <code>assign_detections</code>，再计算误差、门禁及统计。质量标记不偷偷改变主统计。</li><li>晚到按触地处距离线性插值；早到按真零到触地的车速积分。所有 COMPUTED_COUNT 个可计算误差已独立复算；不作额外 timecomp。StartDist 使用前值保持，双轮必须同为 1。</li><li>INPUT_FILE_COUNT 份原始输入逐文件核对 SHA-256。首场景与此前试算逐轮一致；全部总体和场景计数从明细复核。</li></ol><p>核心 SHA-256：<code>CORE_HASH</code></p><p>产品身份：<code>PRODUCT_ID</code></p><p class="warning">该核心不是 ZIP 缺失的两份完整流程；自动 jerk 阈值及通道物理同步尚未人工标定，也不能恢复实车端到端处理/通信延迟。本结果应理解为当前自动候选与原匹配规则下的离线结果。</p></section>
<section><h2>下载与复核</h2><div class="downloads">DOWNLOADS<a href="batch-result.json">完整结果 JSON</a><a href="provenance.json">来源与图表元数据</a></div><p class="small">CSV 以 UTF-8 BOM 导出。19 位 ns 时间戳在 CSV 中加前导单引号防止 Excel 丢失精度；JSON 保留整数。完整压缩包位于本报告目录旁。</p></section></main>
<script>function apply(){const q=document.getElementById('search').value.toLowerCase(),g=document.getElementById('gateFilter').value;let n=0;document.querySelectorAll('table.filterable tbody tr').forEach(r=>{let show=r.textContent.toLowerCase().includes(q)&&(g==='all'||r.dataset.gated===g);r.hidden=!show;if(show)n++});document.getElementById('shown').textContent='清单显示 '+n+' 行（跨清单可重复）'}document.getElementById('search').addEventListener('input',apply);document.getElementById('gateFilter').addEventListener('change',apply);function reveal(){if(location.hash.startsWith('#plot-')){let d=document.querySelector(location.hash);if(d)d.open=true}}window.addEventListener('hashchange',reveal);reveal();apply();</script></html>'''
    replacements={"RUN_ID":html.escape(str(report["run_id"])),"USED_FLAGGED":str(used_flagged),"OVERALL_ROWS":overall_rows,
                  "SCENE_ROWS":"".join(scene_rows),"SPEED_ROWS":speed_rows,"FLAGS_ROWS":flags_rows,
                  "GATED_MISSES":missed_section,"SENS_N":str(sens["accuracy_samples"]),"SENS_P95":fmt(sens["p95_abs_mm"]),
                  "ALL_SECTIONS":"".join(all_sections),"PLOTS":plot_sections,"CORE_HASH":report["core"]["sha256"],
                  "PRODUCT_ID":str(report.get("product_identity")),"DOWNLOADS":downloads}
    replacements.update({"SCENE_COUNT":str(report["scene_count"]),"POOLED_P95":fmt(m["p95_abs_mm"]),
                         "VALID_COUNT":str(m["accuracy_samples"]),"CONTRIBUTING_COUNT":str(contributing),
                         "DETECTED_COUNT":str(m["detected"]),"GATE_COUNT":str(m["gate_eligible"]),
                         "DETECTION_RATE":f"{m['detection_rate']:.2%}" if m["detection_rate"] is not None else "不可用",
                         "GATED_OUT_COUNT":str(m["out_of_range"]),"GATE_FAILED_COUNT":str(m["gate_failed"]),
                         "MISSING_COUNT":str(m["contact_missing"]),"GATED_MISS_COUNT":str(m["unmatched_predictions_gated"]),
                         "NO_VALID_SCENE_COUNT":str(report["scene_count"]-contributing),
                         "GROUP_COUNT":str(sum(len(s.get("front_groups",[])) for s in report["scenes"])),
                         "EXPECTED_COUNT":str(m["expected"]),"CONTACT_COUNT":str(m["contact_available"]),
                         "QUALITY_COUNT":str(len(quality)),"TAIL_COUNT":str(sum(d["kind"]=="malformed_csv_row" for d in quality)),
                         "RESET_COUNT":str(sum(d["kind"]=="large_distance_reset" for d in quality)),
                         "DUPLICATE_GROUP_COUNT":str(sum(s["input"]["timeline"]["conflicting_timestamp_group_count"] for s in report["scenes"] if s["state"]=="CALCULATED")),
                         "COMPUTED_COUNT":str(m["raw_detected_all"]),"INPUT_FILE_COUNT":str(sum(3 for s in report["scenes"] if s["state"]=="CALCULATED"))})
    for key in sorted(replacements,key=len,reverse=True):
        document=document.replace(key,replacements[key])
    (output/"report.html").write_text(document,encoding="utf-8")
    provenance={"source_result":str(result_path.resolve()),"source_result_sha256":hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "core":report["core"],"batch_adapter":report["adapter"],
                "report_builder_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "figure_metadata":figure_meta,"scene_count":report["scene_count"],"contributing_scenes":contributing,
                "figure_alt_text":"Coverage decomposition, per-scene P95, signed valid errors and all raw per-scene jerk/PCM/gate traces. Missing and out-of-range cases remain separately inspectable."}
    (output/"provenance.json").write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    archive=output.with_suffix(".zip")
    if archive.exists():raise FileExistsError(archive)
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED) as stream:
        for path in output.rglob("*"):
            if path.is_file():stream.write(path,arcname=output.name+"/"+str(path.relative_to(output)))
    print(json.dumps({"report":str((output/"report.html").resolve()),"zip":str(archive.resolve()),"figures":len(list((output/"figures").glob("*.png")))},ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("result","output","plot-deps"):parser.add_argument("--"+key,type=Path,required=True)
    args=parser.parse_args()
    build_report(args.result,args.output,args.plot_deps)
