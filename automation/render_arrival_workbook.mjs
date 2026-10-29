// Required XLSX delivery for the explicitly named bench CSV trial adapter.
// Consumes already calculated rows; never detects contacts or reassigns cycles.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import crypto from 'node:crypto';

const opts=Object.fromEntries(Array.from({length:(process.argv.length-2)/2},(_,i)=>[process.argv[2+i*2].slice(2),process.argv[3+i*2]]));
for(const key of ['payload','output','runtime-root','evidence']) if(!opts[key]) throw Error('Missing --'+key);
try {await fs.access(opts.output);throw Error('Output exists; use a new report attempt');} catch(e){if(e.code!=='ENOENT')throw e;}
const require=createRequire(path.join(path.resolve(opts['runtime-root']),'arrival-resolver.cjs'));
const {Workbook,SpreadsheetFile}=await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const r=JSON.parse(await fs.readFile(opts.payload,'utf8'));
if(r.state!=='CALCULATED')throw Error('Incomplete input result');
const wb=Workbook.create(), font='Microsoft YaHei';
const names=['汇总','各场景','速度分组','到点明细','误差分布','异常明细','统计样本','计算说明'];
const sheets=Object.fromEntries(names.map(n=>[n,wb.worksheets.add(n)]));
function col(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
function write(s,row,values){if(values.length)s.getRange(`A${row}:${col(values[0].length-1)}${row+values.length-1}`).values=values.map(r=>r.map(v=>v??null));}
function style(s,lastRow,lastCol,title){
  s.showGridLines=false;
  s.getRange(`A1:${col(lastCol-1)}${Math.max(lastRow,4)}`).format.font={name:font,size:10,color:'#243247'};
  s.getRange(`A1:${col(lastCol-1)}${Math.max(lastRow,4)}`).format.rowHeight=23;
  s.getRange(`A1:${col(lastCol-1)}${Math.max(lastRow,4)}`).format.columnWidth=16;
  s.getRange('A2').values=[[title]];s.getRange('A2').format.font={name:font,size:14,bold:true};
  s.getRange(`A3:${col(lastCol-1)}3`).format.borders={preset:'doubleBottom',style:'thin',color:'#8998AD'};
}
function header(s,row,labels){write(s,row,[labels]);let a=s.getRange(`A${row}:${col(labels.length-1)}${row}`);a.format.fill='#243A59';a.format.font={name:font,size:10,bold:true,color:'#FFFFFF'};a.format.rowHeight=32;a.format.wrapText=true;a.format.horizontalAlignment='center';}
function table(s,row,labels,values,name){header(s,row,labels);write(s,row+1,values);if(values.length)s.tables.add(`A${row}:${col(labels.length-1)}${row+values.length}`,true,name);s.freezePanes.freezeRows(row);}
const detail=sheets['到点明细'];
const labels=['样本ID','场景','前轮','触地存在','双轮配对','配对歧义','门禁 1/0/未知','StartDist FL','StartDist FR','触地 epoch ns','触地相对秒','触地速度 kph','jerk 通道','距离通道','预测周期','真零 epoch ns','状态','带符号误差 mm','绝对误差 mm','纳入精度 1/0','排除原因','质量标记','关联样本','触地类别','左控制 epoch ns','右控制 epoch ns','左距离 mm','右距离 mm','插值间隔 ms','完整场景名','事件序号','距离源文件','jerk 源文件'];
const rows=r.detail, first=6,last=first+rows.length-1;
style(detail,last,labels.length,'到点明细');
detail.getRange('A4').values=[['ns 时间戳为精确文本。1=是，0=否；未知门禁与触地缺失分开保留。']];
const status={late:'晚到',early:'早到',missed:'预测未匹配',contact_unavailable:'触地缺失'};
const sourceByScene=Object.fromEntries(r.scenes.map(s=>[s.scene_id,s.input]));
const matrix=rows.map(x=>[x.sample_id,x.scene_id,x.wheel,+x.contact_available,+x.paired_front_contacts,+x.pair_ambiguous,
 x.gate_eligible===null?'未知':+x.gate_eligible,x.startdist_fl,x.startdist_fr,x.contact_timestamp_ns??'',x.contact_time_s,x.speed_kph,
 `s_InDatCalcFAS_WhlJerk${x.wheel}_mps3`,x.wheel==='FL'?'fl_dist_out_mm_0':'fr_dist_out_mm_0',x.detection_cycle_id??'',x.predicted_zero_timestamp_ns??'',
 status[x.status]??x.status,x.error_mm,null,null,x.exclusion_reason??'',x.flags.join('; '),x.related_samples.join('; '),x.feature_at_contact,
 x.left_timestamp_ns??'',x.right_timestamp_ns??'',x.left_distance_mm,x.right_distance_mm,x.control_bracket_ms,x.scene,x.event,
 sourceByScene[x.scene_id].timeline.path,sourceByScene[x.scene_id].jerk[x.wheel.toLowerCase()].path]);
table(detail,5,labels,matrix,'ArrivalDetail');
detail.getRange(`S${first}:S${last}`).formulas=rows.map((x,i)=>[`=IF(ISNUMBER(R${first+i}),ABS(R${first+i}),"")`]);
detail.getRange(`T${first}:T${last}`).formulas=rows.map((x,i)=>[`=IF(AND(D${first+i}=1,G${first+i}=1,ISNUMBER(R${first+i}),S${first+i}<=500),1,0)`]);
for(const c of ['J','P','Y','Z'])detail.getRange(`${c}${first}:${c}${last}`).setNumberFormat('@');
for(const c of ['K','L','R','S','AA','AB','AC'])detail.getRange(`${c}${first}:${c}${last}`).setNumberFormat('0.00;[Red]-0.00');
detail.getRange('A:A').format.columnWidth=22;detail.getRange('J:J').format.columnWidth=29;detail.getRange('P:P').format.columnWidth=29;
detail.getRange('M:N').format.columnWidth=48;detail.getRange('U:W').format.columnWidth=52;detail.getRange('Y:Z').format.columnWidth=29;
detail.getRange('AD:AD').format.columnWidth=48;detail.getRange('AF:AG').format.columnWidth=100;
detail.freezePanes.freezeColumns(3);
detail.getRange(`G${first}:G${last}`).conditionalFormats.add('cellIs',{operator:'equal',formula:0,format:{fill:'#FFF0D9'}});
detail.getRange(`S${first}:S${last}`).conditionalFormats.add('cellIs',{operator:'greaterThan',formula:500,format:{fill:'#FCE4E4',font:{color:'#9F1D28'}}});

// Each scope retains all relevant raw rows, so exclusion membership is calculated
// in detail and percentile inputs are numeric or blank. No array/spill formulas.
const scopes=[{key:'all',name:'合并',indices:rows.map((_,i)=>i)},
 ...['FL','FR'].map(w=>({key:w,name:w,indices:rows.flatMap((x,i)=>x.wheel===w?[i]:[])})),
 ...[[0,10],[10,20],[20,30],[30,40]].map(([lo,hi])=>({key:`speed${lo}`,name:`${lo}–${hi} kph`,indices:rows.flatMap((x,i)=>x.speed_kph!==null&&x.speed_kph>=lo&&(x.speed_kph<hi||hi===40&&x.speed_kph===40)?[i]:[])})),
 ...r.scenes.map(s=>({key:s.scene_id,name:s.scene_id,indices:rows.flatMap((x,i)=>x.scene_id===s.scene_id?[i]:[])})),
 ...r.scenes.flatMap(s=>['FL','FR'].map(w=>({key:s.scene_id+w,name:s.scene_id+' '+w,indices:rows.flatMap((x,i)=>x.scene_id===s.scene_id&&x.wheel===w?[i]:[])})))];
const samples=sheets['统计样本'];let pos=6;
const sampleCells=[];
for(const scope of scopes){
  const start=pos; const data=scope.indices.map(i=>[scope.name,rows[i].sample_id,null,null]);
  if(data.length){write(samples,pos,data);samples.getRange(`C${pos}:C${pos+data.length-1}`).formulas=scope.indices.map(i=>[`=IF('到点明细'!T${first+i}=1,'到点明细'!R${first+i},"")`]);samples.getRange(`D${pos}:D${pos+data.length-1}`).formulas=scope.indices.map((_,j)=>[`=IF(ISNUMBER(C${pos+j}),ABS(C${pos+j}),"")`]);}
  scope.signed=`'统计样本'!C${start}:C${Math.max(start,pos+data.length-1)}`;
  scope.absolute=`'统计样本'!D${start}:D${Math.max(start,pos+data.length-1)}`;
  sampleCells.push({scope:scope.key,start,end:pos+data.length-1});
  pos+=Math.max(data.length,1)+1;
}
style(samples,pos,4,'统计样本引用');header(samples,5,['统计范围','样本ID','纳入的带符号误差 mm','纳入的绝对误差 mm']);
samples.getRange('A4').values=[['同一轮次可被整体、分轮、速度及场景分别引用；各范围独立统计，禁止跨范围累加。']];
samples.getRange('A:B').format.columnWidth=24;samples.getRange('C:D').format.columnWidth=30;samples.getRange(`C6:D${pos}`).setNumberFormat('0.00;[Red]-0.00');samples.freezePanes.freezeRows(5);
const byKey=Object.fromEntries(scopes.map(s=>[s.key,s]));
function stats(key){let s=byKey[key],v=s.signed,a=s.absolute;return [`=COUNT(${v})`,`=IF(COUNT(${v})=0,"不可用",PERCENTILE.INC(${a},0.9))`,`=IF(COUNT(${v})=0,"不可用",PERCENTILE.INC(${a},0.95))`,`=IF(COUNT(${v})=0,"不可用",AVERAGE(${v}))`,`=IF(COUNT(${v})<2,"不可用",STDEV(${v}))`,`=IF(COUNT(${v})<2,"不可用",VAR(${v}))`,`=COUNTIFS(${v},"<0")`,`=COUNT(${v})-COUNTIFS(${v},"<0")`];}
function ranges(c){return `'到点明细'!$${c}$${first}:$${c}$${last}`;}
function countFor(scope,pairs=[]){let terms=[];if(scope!=='all'){if(['FL','FR'].includes(scope))terms.push(ranges('C'),`"${scope}"`);else if(scope.startsWith('s0'))terms.push(ranges('B'),`"${scope}"`);else{const lo=Number(scope.slice(5)),hi=lo+10;terms.push(ranges('L'),`">=${lo}"`,ranges('L'),`"${hi===40?'<=':'<'}${hi}"`);}}for(const [c,value]of pairs)terms.push(ranges(c),typeof value==='number'?value:`"${value}"`);return terms.length?`=COUNTIFS(${terms.join(',')})`:`=COUNTA(${ranges('A')})`;}
const summary=sheets['汇总'];style(summary,29,10,`${r.scene_count} 场景到点精度`);summary.tabColor='#243A59';
summary.getRange('A4').values=[['独立 CSV 试算适配流程；与缺失的原插件完整流程不等价。']];
header(summary,5,['范围','有效轮次','P90 mm','P95 mm','均值 mm','样本标准差 mm','样本方差 mm²','偏早','偏晚']);
for(const [i,key]of ['all','FL','FR'].entries()){summary.getRange(`A${6+i}`).values=[[byKey[key].name]];summary.getRange(`B${6+i}:I${6+i}`).formulas=[stats(key)];}
summary.getRange('C6:G8').setNumberFormat('0.00;[Red]-0.00');
summary.getRange('A11').values=[['样本分母核对']];header(summary,12,['范围','候选槽位','实际触地','对侧缺失','门禁失败','门禁合格','检出','门禁内未匹配','门禁内超范围','精度样本']);
for(const[i,k]of ['all','FL','FR'].entries()){
 const rr=13+i;summary.getRange(`A${rr}`).values=[[byKey[k].name]];
 summary.getRange(`B${rr}:J${rr}`).formulas=[[countFor(k),countFor(k,[['D',1]]),countFor(k,[['D',0]]),countFor(k,[['G',0]]),countFor(k,[['G',1]]),
 `=${countFor(k,[['G',1],['Q','早到']]).slice(1)}+${countFor(k,[['G',1],['Q','晚到']]).slice(1)}`,countFor(k,[['G',1],['Q','预测未匹配']]),countFor(k,[['G',1],['S','>500']]),countFor(k,[['T',1]])]];
}
summary.getRange('A17').values=[['门禁检出率']];summary.getRange('B17').formulas=[['=IF(F13=0,"不可用",G13/F13)']];summary.getRange('B17').setNumberFormat('0.00%');
const m=r.overall.combined, contributing=r.scenes.filter(s=>s.metrics.combined.accuracy_samples>0).length;
const suspicious=rows.filter(x=>x.gate_eligible===true&&x.status==='missed'&&x.flags.includes('nearby_cycle_assigned_elsewhere')).length;
const notes=[
 `${m.gate_eligible} 个门禁合格轮次中 ${m.unmatched_predictions_gated} 个未匹配、${m.detected} 个检出；${m.out_of_range} 个超范围，${m.accuracy_samples} 个进入精度统计。`,
 `${contributing} 个场景有精度样本；其余 ${r.scene_count-contributing} 个显示不可用。整体合并轮次，不平均场景 P95。`,
 `门禁内 ${suspicious} 条未匹配有附近预测周期被较早候选占用的嫌疑，不能直接认定模型漏检。`,
 '自动 jerk 候选尚未人工确认；XCP 物理同步偏移未标定。没有设置验收阈值，准入未判定。',
 '本批 DIT、宽高、补偿对比不可用；后处理不改原始数据，不追加时间补偿。',
 '显示精度 2 位小数；底层数值保留精度。模型误差为输入结果，统计公式可核对。'];
for(let i=0;i<notes.length;i++)summary.getRange(`A${18+i}`).values=[[notes[i]]];
summary.getRange('A:A').format.columnWidth=19;summary.getRange('B:J').format.columnWidth=19;

const scene=sheets['各场景'];style(scene,r.scenes.length+5,14,'各场景精度与覆盖');
header(scene,5,['场景','录波时间','实际触地','门禁失败','门禁合格','门禁内未匹配','门禁内超范围','有效轮次','FL P95 mm','FR P95 mm','合并 P90 mm','合并 P95 mm','均值 mm','样本标准差 mm']);
for(const[i,s]of r.scenes.entries()){
 const rr=6+i,ss=stats(s.scene_id);
 scene.getRange(`A${rr}:B${rr}`).values=[[s.scene_id,s.scene.split('_')[1].replaceAll('-',':')]];
 scene.getRange(`C${rr}:N${rr}`).formulas=[[countFor(s.scene_id,[['D',1]]),countFor(s.scene_id,[['G',0]]),countFor(s.scene_id,[['G',1]]),countFor(s.scene_id,[['G',1],['Q','预测未匹配']]),countFor(s.scene_id,[['G',1],['S','>500']]),ss[0],stats(s.scene_id+'FL')[2],stats(s.scene_id+'FR')[2],ss[1],ss[2],ss[3],ss[4]]];
}
scene.getRange(`I6:N${5+r.scenes.length}`).setNumberFormat('0.00;[Red]-0.00');scene.getRange(`I6:N${5+r.scenes.length}`).format.horizontalAlignment='right';scene.getRange('A:N').format.columnWidth=19;scene.freezePanes.freezeRows(5);scene.freezePanes.freezeColumns(2);
const speed=sheets['速度分组'];style(speed,11,11,'实际触地速度分组');
speed.getRange('A4').values=[['分组左闭右开；最后一组包含 40 kph。无触地速度的缺失槽位不归组。']];
header(speed,5,['速度范围','实际触地','门禁合格','已检出','门禁内超范围','有效轮次','P90 mm','P95 mm','均值 mm','样本标准差 mm','样本方差 mm²']);
for(const[i,lo]of [0,10,20,30].entries()){const key='speed'+lo,ss=stats(key),rr=6+i;speed.getRange(`A${rr}`).values=[[byKey[key].name]];speed.getRange(`B${rr}:K${rr}`).formulas=[[countFor(key,[['D',1]]),countFor(key,[['G',1]]),`=${countFor(key,[['G',1],['Q','早到']]).slice(1)}+${countFor(key,[['G',1],['Q','晚到']]).slice(1)}`,countFor(key,[['G',1],['S','>500']]),...ss.slice(0,6)]];}
speed.getRange('G6:K9').setNumberFormat('0.00;[Red]-0.00');speed.getRange('A:K').format.columnWidth=20;

const dist=sheets['误差分布'], valid=rows.filter(x=>x.used_for_accuracy).map(x=>x.error_mm);
const lo=valid.length?Math.floor(Math.min(...valid)/10)*10:0,hi=valid.length?Math.floor(Math.max(...valid)/10)*10+10:10;
const bins=Array.from({length:(hi-lo)/10},(_,i)=>[lo+10*i,lo+10*(i+1),String(lo+10*i),null]);
style(dist,Math.max(29,6+bins.length),14,'带符号误差分布');dist.getRange('A4').values=[['10 mm 分箱，左闭右开；负值偏早，正值偏晚，仅纳入精度样本。']];
header(dist,5,['下界 mm','上界 mm','横轴 mm','轮次数']);write(dist,6,bins);
dist.getRange(`D6:D${5+bins.length}`).formulas=bins.map((_,i)=>[`=COUNTIFS(${ranges('T')},1,${ranges('R')},">="&A${6+i},${ranges('R')},"<"&B${6+i})`]);
const chart=dist.charts.add('bar',dist.getRange(`C5:D${5+bins.length}`));chart.title='到点误差分布（10 mm 分箱）';chart.hasLegend=false;chart.titleTextStyle.typeface=font;chart.titleTextStyle.fontSize=14;
chart.xAxis={axisType:'textAxis',textStyle:{typeface:font,fontSize:11},tickLabelInterval:5};chart.yAxis={numberFormatCode:'0.0',numberFormatSourceLinked:false,textStyle:{typeface:font,fontSize:11}};
chart.xAxis.title.text='带符号误差 mm（箱下界）';chart.yAxis.title.text='轮次数';chart.series.items[0].fill='#386CB0';chart.setPosition('F5','O26');
dist.getRange('A:D').format.columnWidth=17;dist.getRange(`C6:C${5+bins.length}`).format.horizontalAlignment='right';

const anomaly=sheets['异常明细'];const exceptionIndices=rows.flatMap((x,i)=>(x.flags.length||x.gate_eligible===false||x.status==='missed'||!x.contact_available||(x.error_mm!==null&&Math.abs(x.error_mm)>500))?[i]:[]);
const alabel=['样本ID','场景','轮','触地存在','门禁','状态','带符号误差 mm','纳入精度','排除原因','质量标记','关联样本'];
style(anomaly,exceptionIndices.length+40,11,'未匹配、门禁失败及异常清单');anomaly.getRange('A4').values=[['各类异常可重叠；质量标记不自动排除精度样本。按门禁与状态筛选。']];
const acols=['A','B','C','D','G','Q','R','T','U','V','W'];
table(anomaly,5,alabel,exceptionIndices.map(()=>Array(11).fill(null)),'ArrivalExceptions');
if(exceptionIndices.length)anomaly.getRange(`A6:K${5+exceptionIndices.length}`).formulas=exceptionIndices.map(i=>acols.map(c=>`=IF(ISBLANK('到点明细'!${c}${first+i}),"",'到点明细'!${c}${first+i})`));
anomaly.getRange('A:A').format.columnWidth=24;anomaly.getRange(`G6:G${Math.max(6,5+exceptionIndices.length)}`).setNumberFormat('0.00;[Red]-0.00');anomaly.getRange('I:K').format.columnWidth=64;
const quality=r.scenes.flatMap(s=>s.data_quality),qr=exceptionIndices.length+9;
header(anomaly,qr,['场景','原始数据问题','轮','epoch ns','源行号','说明']);write(anomaly,qr+1,quality.map(x=>[x.scene_id,x.kind,x.wheel,String(x.timestamp_ns??''),x.source_line??'',x.reason??JSON.stringify(x)]));
if(quality.length)anomaly.getRange(`D${qr+1}:D${qr+quality.length}`).setNumberFormat('@');

const methods=sheets['计算说明'];style(methods,37,3,'计算规则、适配边界与来源');
const ruleRows=[
 ['执行引擎','bench_csv_trial_v1','当前已确认的离线试算口径；不是原插件 native 模式'],
 ['原插件模式','不可用','原 original/startdist 两份流程缺失，不能宣称完整等价'],
 ['StartDist','启用','每个实际触地时双轮槽位 0 均为 1，前值保持'],
 ['DIT','不可用','CSV/JSON 没有 MDF remark，不根据文件名推断'],
 ['信号范围','前轮 FL/FR','槽位 0 不是 PCM Method 0；不扩展到后轮'],
 ['输出时间','payload_ctrl_ts_ms','同时间取 publish_seq 最大且 dds_write_ok=1 的整条消息'],
 ['距离字段','fl/fr_dist_out_mm_0','本批已核对实际输出含义，不冒充 FAS/EndDist 方法名'],
 ['触地候选','原核心 jerk + speed','独立检测；未启用预测距离/类别引导修正'],
 ['候选分组','固定 1 秒前轮峰时差','有序最大配对数后最小时间差；缺失对侧保留，配对歧义标记'],
 ['预测匹配','原核心 assign_detections','全部候选先匹配，再计算门禁；不回收被较早候选占用的周期'],
 ['晚到','触地时距离线性插值','正号为晚到'],
 ['早到','真零至触地车速积分的负值','负号为早到'],
 ['统计纳入','门禁通过、已检出且 |误差| ≤ 500 mm','所有门禁失败、未匹配、超范围与触地缺失行保留'],
 ['分位数','绝对误差 PERCENTILE.INC','整体合并轮次；不平均场景 P95'],
 ['均值及离散程度','带符号误差','STDEV / VAR 为样本标准差/方差，ddof=1；n<2 不可用'],
 ['额外补偿','未执行','无有效补偿对比输入，不把缺失写成无变化'],
 ['宽度/高度','本批不可用','没有可核实的对应输入'],
 ['验收结论','未判定','未提供正式阈值'],
 ['人工核验','尚未完成','自动 jerk 候选未确认为减速带；XCP 物理偏移未标定'],
 ['刷新方式','重新运行独立评测','本表为该次输入快照；修改场景、速度或候选后需重新生成分组'],
 ['来源 Run ID',r.run_id,'已存在的工作站预测'],
 ['产品身份',r.product_identity,'与下载收据核对'],
 ['核心 SHA-256',r.core.sha256,'原核心文件未改动'],
 ['适配器 SHA-256',r.adapter.sha256,'输入适配、批量与质量标记版本'],
 ['来源收据 SHA-256',r.source_receipt.sha256,`${r.scene_count*3} 份文件的哈希验证记录`],
];
header(methods,5,['项目','规则或值','说明']);write(methods,6,ruleRows);methods.getRange('A:A').format.columnWidth=26;methods.getRange('B:B').format.columnWidth=90;methods.getRange('C:C').format.columnWidth=96;

await fs.mkdir(opts.evidence,{recursive:true});
wb.recalculate();
const checks={};
for(const[name,range]of [['汇总','A5:J15'],['各场景','A5:N23'],['速度分组','A5:K9'],['到点明细','A5:T8']]){
 const q=await wb.inspect({kind:'table',range:`'${name}'!${range}`,include:'values,formulas',tableMaxRows:20,tableMaxCols:20,maxChars:9000});
 checks[name]=q.ndjson;
}
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:100},summary:'Final formula error scan'});
checks.formula_errors=errors.ndjson;
if(!errors.ndjson.includes('matched 0 entries'))throw Error('Workbook formula error scan failed: '+errors.ndjson);
await fs.writeFile(path.join(opts.evidence,'inspect.json'),JSON.stringify(checks,null,2));
const got=summary.getRange('B6:I8').values;
const expect=['combined','FL','FR'].map(k=>{let m=r.overall[k];return [m.accuracy_samples,m.p90_abs_mm,m.p95_abs_mm,m.signed_mean_mm,m.sample_std_mm,m.sample_variance_mm2,m.early,m.late];});
for(let i=0;i<3;i++)for(let j=0;j<8;j++)if(expect[i][j]===null ? got[i][j]!=='不可用' : typeof got[i][j]!=='number'||Math.abs(got[i][j]-expect[i][j])>1e-6)throw Error(`Workbook reconciliation failed at summary ${i},${j}: ${got[i][j]} vs ${expect[i][j]}`);
if(!detail.getRange(`T${first}:T${last}`).values.every((v,i)=>v[0]===+rows[i].used_for_accuracy))throw Error('Detail population mismatch');
if(exceptionIndices.length){
 const values=anomaly.getRange(`A6:K${5+exceptionIndices.length}`).values;
 exceptionIndices.forEach((i,j)=>{for(const k of [3,4,7]){const expected=matrix[i][({3:3,4:6,7:19})[k]]??+rows[i].used_for_accuracy;if(values[j][k]!==expected)throw Error('Exception zero/blank mismatch');}});
}
const histTotal=dist.getRange(`D6:D${5+bins.length}`).values.reduce((a,b)=>a+b[0],0);
if(histTotal!==r.overall.combined.accuracy_samples)throw Error('Distribution count mismatch');
for(let i=0;i<r.scenes.length;i++){
 const val=scene.getRange(`H${6+i}:N${6+i}`).values[0],m=r.scenes[i].metrics;
 const target=[m.combined.accuracy_samples,m.FL.p95_abs_mm,m.FR.p95_abs_mm,m.combined.p90_abs_mm,m.combined.p95_abs_mm,m.combined.signed_mean_mm,m.combined.sample_std_mm];
 val.forEach((v,j)=>{if(target[j]===null ? v!=='不可用' : typeof v!=='number'||Math.abs(v-target[j])>1e-6)throw Error(`Scene reconciliation failed ${i},${j}`);});
}
const rendered=[];
for(const[name,range]of [['汇总','A1:J24'],['各场景','A1:N23'],['速度分组','A1:K10'],['到点明细','A1:L11'],['误差分布','A1:O29'],['异常明细','A1:H12'],['统计样本','A1:D15'],['计算说明','A1:C17']]){
 const img=await wb.render({sheetName:name,range,scale:1,format:'png'});
 const file=path.join(opts.evidence,name+'.png');await fs.writeFile(file,new Uint8Array(await img.arrayBuffer()));rendered.push(file);
}
await fs.mkdir(path.dirname(opts.output),{recursive:true});
const output=await SpreadsheetFile.exportXlsx(wb);await output.save(opts.output);
await fs.writeFile(path.join(opts.evidence,'validation.json'),JSON.stringify({state:'VERIFIED',summary_reconciled:true,all_scene_metrics_reconciled:true,detail_membership_reconciled:true,rendered_sheets:rendered,chart_count:1,chart_series:chart.series.items.map(s=>({values:s.formula,categories:s.categoryFormula})),sha256:crypto.createHash('sha256').update(await fs.readFile(opts.output)).digest('hex')},null,2));
console.log(JSON.stringify({state:'EXPORTED',output:path.resolve(opts.output),scenes:r.scene_count,samples:r.overall.combined.accuracy_samples}));
