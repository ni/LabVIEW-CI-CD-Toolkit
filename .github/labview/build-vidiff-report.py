#!/usr/bin/env python3
import argparse
from html import escape
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET


GROUPS = (
    ('fpfunc', 'Front panel functional changes', 'fpChanges/funcChanges'),
    ('fpcosm', 'Front panel position/size changes', 'fpChanges/cosmChanges'),
    ('bdfunc', 'Block diagram functional changes', 'bdChanges/funcChanges'),
    ('bdcosm', 'Block diagram cosmetic changes', 'bdChanges/cosmChanges'),
    ('atr', 'VI attribute changes', 'attrChanges/attrChange'),
)


def image_cell(source, image_path, label):
    if not image_path:
        return '<td><p>No image supplied.</p></td>'
    image = (source.parent / image_path).resolve()
    if not image.is_relative_to(source.parent.resolve()):
        raise ValueError(f'Unsafe comparison image: {image_path}')
    if not image.is_file():
        return '<td><p>LabVIEW did not generate an image for this change.</p></td>'
    url = escape(quote(image_path, safe='/'), quote=True)
    return (f'<td><a href="{url}"><img loading="lazy" src="{url}" '
            f'alt="{label}"></a></td>')


def render_report(source, destination):
    root = ET.parse(source).getroot()
    comparisons = root.findall('./detailInfo/comparedVIs')
    if root.tag != 'report' or not comparisons:
        raise ValueError('Not a LabVIEW comparison XML report')
    sections = []
    for comparison_index, comparison in enumerate(comparisons, 1):
        prefix = f'comparison-{comparison_index}-'
        paths = [escape(comparison.get(f'path{number}', '')) for number in (1, 2)]
        sections.append(f'<section><h2>Compared VIs</h2><dl><dt>Before</dt><dd>{paths[0]}</dd>'
                        f'<dt>After</dt><dd>{paths[1]}</dd></dl>')
        sections.append('<nav aria-label="Change summary"><ul>')
        for anchor, title, query in GROUPS:
            changes = comparison.findall(query)
            sections.append(f'<li><a href="#{prefix}{anchor}">{title}: {len(changes)}</a></li>')
        sections.append('</ul></nav>')
        for anchor, title, query in GROUPS:
            changes = comparison.findall(query)
            sections.append(f'<section id="{prefix}{anchor}"><h2>{title} ({len(changes)})</h2>')
            if not changes:
                sections.append('<p>No changes reported in this category.</p>')
            for change_index, change in enumerate(changes, 1):
                description = ' — '.join(change.get(key, '') for key in
                                         ('objType', 'objLabel', 'attrType', 'diffType')
                                         if change.get(key))
                sections.append(f'<article><h3>{change_index}. {escape(description or "Change")}</h3>')
                if change.get('detailDesc'):
                    sections.append(f'<pre>{escape(change.get("detailDesc"))}</pre>')
                if change.get('imagePath1') or change.get('imagePath2'):
                    sections.append('<table><thead><tr><th>Before</th><th>After</th></tr></thead><tbody><tr>')
                    for number, label in ((1, 'Before'), (2, 'After')):
                        sections.append(image_cell(source, change.get(f'imagePath{number}'), label))
                    sections.append('</tr></tbody></table>')
                sections.append('</article>')
            sections.append('</section>')
        sections.append('</section>')
    generated = root.find('basicInfo')
    timestamp = escape(generated.get('genTime', '')) if generated is not None else ''
    xml_url = escape(quote(source.name), quote=True)
    document = '''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VI comparison report</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif;background:light-dark(#fff,#0d1117);color:light-dark(#1f2328,#e6edf3)}
body{margin:0 auto;padding:24px;max-width:1600px}a{color:light-dark(#0969da,#58a6ff)}
h1{font-size:1.6rem}h2{font-size:1.2rem;margin-top:28px}h3{font-size:1rem}
article{border:1px solid light-dark(#d0d7de,#30363d);border-radius:8px;margin:12px 0;padding:16px}
dl{display:grid;grid-template-columns:auto 1fr;gap:8px}dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}
nav ul{display:flex;flex-wrap:wrap;gap:12px 24px;padding-left:20px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.5}
table{width:100%;table-layout:fixed;border-collapse:collapse}th{text-align:left;padding:8px}
td{vertical-align:top;padding:8px;overflow:auto}img{max-width:100%;height:auto;background:white}
.note{font-size:.9rem;opacity:.8}
</style></head><body><h1>VI comparison report</h1>
'''
    document += (f'<p class="note">Generated {timestamp} from LabVIEW XML comparison data. '
                 f'<a href="{xml_url}">Download source XML</a>. '
                 'Click a screenshot to view its full resolution.</p>')
    document += '\n'.join(sections) + '\n</body></html>\n'
    destination.write_text(document, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='Render LabVIEW comparison XML as static HTML')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    arguments = parser.parse_args()
    render_report(arguments.source, arguments.destination)


if __name__ == '__main__':
    main()
