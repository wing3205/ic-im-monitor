#!/bin/zsh
set -u
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print '请先按 README.md 建立 Python 环境。'
  read 'answer?按回车退出'
  exit 1
fi
.venv/bin/python monitor.py --no-push
result=$?
if [[ -f output/index.html ]]; then
  open output/index.html
fi
if [[ $result != 0 ]]; then
  print '本次存在数据异常，已保留历史。请查看上方提示与报告中的失败记录。'
fi
read 'answer?按回车关闭窗口'
exit $result
