# 部署与验收记录

验收日期：2026-10-21（北京时间）。本页为验收快照，当前状态以GitHub仓库变量和Actions运行记录为准。

- 公开报告：https://wing3205.github.io/ic-im-monitor/
- 公开仓库：https://github.com/wing3205/ic-im-monitor
- 部署分支：`codex/ic-im-monitor-v2`
- 工作流：`.github/workflows/daily.yml`，`IC IM spread monitor`，已激活。
- 计划时间：`37 18 * * 1-5`，`timezone: Asia/Shanghai`。交易日18:37计划执行，GitHub调度可能延迟；休市不推送旧行情。
- `ENABLE_PUSH=true`、`ENABLE_PAGES=true`、`REPORT_BASE_URL=https://wing3205.github.io/ic-im-monitor/`。
- SendKey仅保存在GitHub Actions Secret `SERVERCHAN_SENDKEY`；源码、报告及本记录不含其值。

## 已完成的验证

1. 本地22项测试通过；桌面1280px、手机390px和375px交互与下载检查通过。
2. 官方历史覆盖2025-10-20至2026-10-21，共244个交易日、488条IC/IM指标；按当前核对的休市表区间内缺失0日。
3. 首次云端测试及页面发布通过：https://github.com/wing3205/ic-im-monitor/actions/runs/39612557074
4. 强制绕过缓存访问中金所，最近10个交易日全部抓取成功，失败0日；报告、Pages与接入测试推送作业全部成功：https://github.com/wing3205/ic-im-monitor/actions/runs/39612852418
5. 接收端已确认收到「IC/IM 监控接入测试」，且手机可打开报告链接。
6. 报告与PNG公网地址均验证HTTP 200，页面包含2026-10-21和488条历史指标。

## 日常使用与维护

- 微信接收摘要，点链接查看图表；页面支持1个月/半年/1年/全部区间、双口径切换和CSV下载。
- 默认文章对照口径：挂牌最远两个连续季月；辅助口径：最近两个季月。以页面显示的具体合约和日期为准。
- 当前为通用观察，未配置个人持仓；分位、到期和流动性提示不构成针对账户的交易指令。
- 暂停自动发送：仓库Settings → Secrets and variables → Actions → Variables，将 `ENABLE_PUSH` 改为 `false`；历史与报告保留。
- 更换密钥：在同一位置更新Secret `SERVERCHAN_SENDKEY`，不修改源码。
- 任务异常：查看Actions最新运行。已预留或结果不确定的消息不会自动重发；先查微信和Server酱日志，再决定是否手动勾选 `retry_push`。
- 休市表目前核对至2026年；2027年安排发布后需更新 `trading_calendar.json`，并核对特殊交割调整。
- 代码/日历/依赖更新会自动测试并重新发布页面，不自动发送微信；手动发送仍须显式选择。数据和状态自动提交不会触发重复构建。
