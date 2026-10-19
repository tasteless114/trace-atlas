# vendor/

Bundled third-party code, so the app runs on a plain Python installation with **no
`pip install` and no network access**.

| Folder | Package | Version | License |
| --- | --- | --- | --- |
| `fitparse/` | [python-fitparse](https://github.com/dtcooper/python-fitparse) | 1.2.0 | MIT, see `fitparse/LICENSE.txt` |

`fitparse` is pure Python and has no dependencies of its own. Only its `.py` files are
kept; `profile.py` is the FIT field/message table that makes decoding standard-compliant.

It is imported from `heatmap.py` after adding this folder to `sys.path`, so the vendored
copy always wins over anything installed system-wide. To update it, replace the `.py`
files with a newer release and keep its license file.
