"""Create a portable, data-free ZIP using an explicit source-file allowlist."""
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = [
    'server.py', 'heatmap.py', 'tiles.py', 'README.md', '.gitignore',
    'LICENSE-COORD-TRANSFORM.txt', 'start.ps1', 'stop.ps1',
    '启动行迹.cmd', '停止行迹.cmd', 'build_share.py', '打包分享版.cmd',
]
# Run-from-source folders; vendor/ carries the bundled FIT decoder so nothing is installed.
FOLDERS = ['static', 'templates', 'tests', 'vendor']
ACTIVITY_FOLDERS = ['运动数据']
SOURCE_SUFFIXES = {'.js', '.css', '.html', '.txt', '.md', '.py', '.png'}
# 朋友解压后立刻能看到画面：把合成示例轨迹放进 运动数据/。
DEMO_FOLDER = '示例数据'
DEMO_SUFFIXES = {'.gpx', '.tcx', '.fit'}


def build_share(output=None):
    output = Path(output) if output else ROOT / '行迹_分享版.zip'
    prefix = '行迹/'
    files = [ROOT / name for name in FILES]
    for directory in FOLDERS:
        files.extend(p for p in (ROOT / directory).rglob('*') if p.is_file() and
                     '__pycache__' not in p.parts and p.suffix.lower() in SOURCE_SUFFIXES)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix, '')
        for folder in ACTIVITY_FOLDERS:
            archive.writestr(prefix + folder + '/', '')
        for file in files:
            archive.write(file, prefix + file.relative_to(ROOT).as_posix())
        # 示例轨迹：一份放 示例数据/ 留档，一份直接放进 运动数据/ 好让朋友一打开就有画面。
        demo = ROOT / DEMO_FOLDER
        if demo.is_dir():
            for item in sorted(demo.iterdir()):
                if not item.is_file() or item.suffix.lower() not in DEMO_SUFFIXES:
                    continue
                data = item.read_bytes()
                archive.writestr(prefix + DEMO_FOLDER + '/' + item.name, data)
                archive.writestr(prefix + '运动数据/' + item.name, data)
        archive.writestr(prefix + 'config.json', json.dumps({
            'port': 8787, 'folder': '运动数据',
            'coordinate_mode': 'auto', 'basemap': 'amap_road', 'workers': 0,
            'offline_min_zoom': 8, 'offline_max_zoom': 16, 'offline_max_tiles': 12000,
        }, ensure_ascii=False, indent=2) + '\n')
    return output


if __name__ == '__main__':
    print(build_share())
