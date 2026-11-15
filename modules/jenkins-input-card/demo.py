"""Generate offline Card 2.0 examples and a static HTML preview outside the repo."""
import argparse
import html
import json
from pathlib import Path

from jenkins_input_card import Identity, InputCards, CardError


def find_button(view, prefix):
    return next(item['name'] for element in view['card']['body']['elements'] if element['tag'] == 'form'
                for item in element['elements'] if item['tag'] == 'button' and item['name'].startswith(prefix + '__'))


def preview(view, title):
    content = []
    for element in view['card']['body']['elements']:
        if element['tag'] == 'form':
            for item in element['elements']:
                if item['tag'] == 'input':
                    label = html.escape(item['label']['content'])
                    value = html.escape(item['default_value'])
                    content.append(f'<label>{label}</label><textarea readonly rows="'
                                   f'{7 if item["name"] == "app_version" else 2}">{value}</textarea>')
                elif item['tag'] == 'button':
                    content.append('<button disabled>' + html.escape(item['text']['content']) + '</button>')
        elif element['tag'] == 'markdown':
            content.append('<p>' + html.escape(element['content']).replace('**', '') + '</p>')
        elif element['tag'] == 'div':
            content.append('<p class="error">' + html.escape(element['text']['content']) + '</p>')
    return '<section><header>' + html.escape(title) + '</header><div class="body">' + ''.join(content) + '</div></section>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New empty output directory outside the repository')
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if root.exists() and any(root.iterdir()):
        raise CardError('示例输出目录必须为空，避免覆盖已有个人配置')
    store = InputCards(root / 'demo-preferences.sqlite3')
    alice, bob = Identity('offline-demo', 'alice'), Identity('offline-demo', 'bob')
    first = store.open(alice)
    invalid_fields = {'branch_link': 'https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-315-eval',
                      'app_version': 'conan_shared/@livc/release-schu\nuroad_m100/@livc/stable\ndag_compiler/@livc/stable'}
    invalid = store.submit(alice, find_button(first, 'check'), invalid_fields)
    submitted = {**invalid_fields, 'app_version': 'conan_shared/0.0.0-demo@livc/release-schu\n'
                 'uroad_m100/0.0.0-demo@livc/stable\ndag_compiler/0.0.0-demo@livc/stable\n'
                 'anti_pinch/0.0.0-demo@livc/stable'}
    checked = store.submit(alice, find_button(invalid, 'check'), submitted)
    saved = store.submit(alice, find_button(checked, 'save'), submitted)
    restored = store.open(alice)
    other = store.open(bob)
    views = [('first-use', first, '首次使用 · 空版本模板'),
             ('incomplete', invalid, '未填版本 · 保留编辑内容并提示'),
             ('checked', checked, '修改并追加组件 · 格式检查通过'),
             ('saved', saved, '本人确认 · 仅保存配置'),
             ('restored', restored, '再次打开 · 自动带出本人配置'),
             ('other-user', other, '另一位用户 · 仍为空版本模板')]
    for name, view, _ in views:
        (root / (name + '.card.json')).write_text(json.dumps(view['card'], ensure_ascii=False, indent=2), encoding='utf-8')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jenkins 输入卡片 · 离线预览</title>
<style>body{margin:0;background:#f3f5f8;color:#1f2937;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1240px;margin:36px auto;padding:0 20px}h1{font-size:27px;margin-bottom:6px}.note{color:#596579}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:22px;margin-top:26px}
section{background:white;border:1px solid #dce3ed;border-radius:12px;overflow:hidden;box-shadow:0 3px 12px #172b4d08}
header{background:#e8f0ff;color:#244ea4;font-size:17px;font-weight:650;padding:16px 20px}.body{padding:8px 20px 20px}
p{font-size:14px;color:#607087;white-space:pre-wrap}label{display:block;margin:16px 0 7px;font-weight:600;font-size:14px}
textarea{box-sizing:border-box;width:100%;padding:10px;border:1px solid #cdd7e5;border-radius:7px;resize:none;
font:13px/1.7 ui-monospace,Consolas,monospace;background:#fbfcff;color:#26364d}
button{width:100%;margin-top:14px;padding:10px;border:0;border-radius:7px;background:#376df6;color:white;font-size:14px}
.error{color:#bb3030;background:#fff1f1;padding:10px;border-radius:6px}</style><main>
<h1>Jenkins 发包配置</h1><div class="note">独立模块 · 本人配置记忆 · 无预填按钮</div>
<p>以下由真实卡片 JSON 生成的静态示意，不是飞书客户端渲染；按钮在此预览中不可点击。
示例用户和 0.0.0-demo 版本均为离线演示，不表示存在对应制品。实际流程由本地测试验证，未连接 Bot 或 Jenkins。</p>
<div class="grid">'''
    document += ''.join(preview(view, title) for _, view, title in views) + '</div></main></html>'
    (root / 'preview.html').write_text(document, encoding='utf-8')
    print(json.dumps({'output': str(root), 'cards': len(views), 'preview': str(root / 'preview.html'),
                      'external_calls': 0, 'builds_submitted': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
