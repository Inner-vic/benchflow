"""Fixed local RoadModel plugin; no credentials or additional connection."""
import sys
from pathlib import Path
ROOT = Path('D:\\developer\\Desktop\\Project\\RoadModelEval')
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from pcm_eval.feishu_entry import handle, recover, tick, deliver
