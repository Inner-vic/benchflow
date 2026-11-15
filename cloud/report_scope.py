"""Read actual scene coverage; do not infer scope from the requested count."""
import re


def parse_scene_coverage(raw, expected=None):
    match = re.search(r'评测场景数:\s*(\d+)', raw)
    rows = []
    for index, scene, completed, total in re.findall(
            r'^\s*\[(\d+)\]\s+([^\s:]+):\s*(\d+)/(\d+)', raw, re.MULTILINE):
        rows.append({'index': int(index), 'scene': scene,
                     'completed_frames': int(completed), 'truth_frames': int(total)})
    names = [row['scene'] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError('duplicate scene coverage in report')
    count = int(match.group(1)) if match else None
    if rows and count is not None and count != len(rows):
        raise ValueError('report scene count differs from coverage rows')
    if expected is not None:
        if count != len(expected) or set(names) != set(expected):
            raise ValueError('report does not cover exactly the selected scenes')
        if any(r['truth_frames'] <= 0 or r['completed_frames'] > r['truth_frames'] for r in rows):
            raise ValueError('invalid per-scene frame coverage')
    return {'scene_count': count, 'scenes': rows}
