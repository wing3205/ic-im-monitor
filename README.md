# IC / IM 贴水观察 V2

在线报告：https://wing3205.github.io/ic-im-monitor/

本仓库已完成部署、云端行情验证和微信实收确认；运行配置与验收证据见 [DEPLOYMENT.md](DEPLOYMENT.md)。

每日读取中金所具体月份合约盘后收盘价，保存原始 IC/IM 行情快照和一年以上的历史，生成手机可读的交互报告及 PNG 图，并可通过 Server酱发送摘要。未接入交易下单。

## 当前状态

- 公开发布、发送消息和启用定时发送均需用户明确确认；GitHub仓库变量控制是否实际启用。
- 不带发送参数的云端生成不发送；本仓库微信变量 `ENABLE_PUSH=true`。工作日18:37计划运行，20:37、22:37再检查补漏；已提交的同渠道、同交易日日报不会重复发送。GitHub调度可能延迟。
- 报告默认使用挂牌最远两个连续季月，作为文章对照；同时提供最近两个季月辅助口径，始终显示具体合约。多数截图日期用远端口径可复现到一位小数，截图最后一日仍有差异，原作者取价时点未获确认。
- 年化指标是 `(下季收盘价 / 当季收盘价 - 1) * 4 * 100`，单位为百分比，与现货基差、账户收益和实际移仓成交价差分别不同。

## 本地使用

需要 Python 3.12+。进入本项目目录后：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v tests.py
python monitor.py --demo
python monitor.py --no-push
```

本机已准备 `.venv`，可直接双击「更新本地报告.command」，只更新数据和报告，绝不发送微信。

历史补录可以在非交易日执行，已成功下载的日期逐日保存；再次运行优先读取缓存，失败不会清空已有数据。例如：

```bash
.venv/bin/python monitor.py --date 2026-10-20 --backfill-days 365 --backfill-only --no-push
```

`--backfill-only` 在末日没有行情时仍保存此前结果。存在未成功的请求会退出码 2，并在 `data/ingest_status.json` 列出日期；修复网络后重新执行即可补漏。`--refresh` 重新请求所选范围，用于官方修订。

日常不指定日期会核查近14个自然日并补漏。报告日期按北京时间最近可用的晚间交易日选择：18:00以前回到上一交易日，18:00以后可尝试当天，周末和节假日向前查找；18:00只是保守取数边界，不保证行情已发布。这样跨午夜延迟的任务仍生成前一交易日日报。发送正文、数据完整性及云端固定的 `REPORT_DATE` 必须一致；旧于当前可用交易日或未来的报告不发送，不把生成时间冒充行情日期。未知年份休市表只能估算，需提前补全交易日历。

## 输出

- `data/quotes/YYYY-MM-DD.json`：当日所有有效 IC/IM 合约、收盘/结算价、量仓及实际来源 URL、响应哈希、抓取时间。
- `data/history.csv`：按交易日和品种去重的季度指标；无前缀列为近端季月，`reference_`列为文章对照的远端季月。后到的有效数据修正同日条目。
- `data/ingest_status.json`：本次补录范围、成功和失败日期。
- `data/push_state.json`：提交状态和摘要哈希，不含 SendKey；包含发送预留，用于防止重复消息。
- `output/index.html`：数据和SVG内嵌的手机交互报告，无外部字体/CDN，可离线打开；含1个月、半年、1年、全部切换、点击读数及完整CSV下载。
- `output/quarter_spread.png`：可供微信正文引用的图片。
- `output/notification.md`：拟发送的正文，可先审查。

历史曲线在换季或缺失交易日处断开；同组合约分位从完整报价快照还原此前最多90次观测，不限于该组合已被选作每日指标的时期。另列滚动365日深度分位，使用相同选约规则的每日指标，具体合约组可以不同。两者均不包含当天、每日最多一次、至少20次才显示，并标注样本数及区间。深度分位越高表示相对所选历史折价越深，不是胜率；新合约挂牌前没有成交价，不能靠补旧数据制造同组样本。

当前使用“暂未持仓、学习与模拟”模式，新增每天的数据解读、历史位置解释、模拟记录练习和一手名义价值/价格损益示例。解说由确定性规则生成，**未调用大模型 API**，无需新增 API 账号或费用；双口径切换会同步切换数据与学习解说。未读取账户或生成个人仓位指令。分位未按剩余期限和季节校准，也未进行策略回测；80%/20%仅为同组观察阈值，滚动分位不作为买卖触发器。

## 微信发送

需要已绑定微信的 Server酱 Turbo SendKey（SCT）或 Server酱³（sctp）。不把密钥写入源码、命令行参数、报告或仓库。

本地仅在确认发送后，将密钥设置为环境变量 `SERVERCHAN_SENDKEY`，执行 `python monitor.py`。不要将真实密钥复制进本说明。程序对历史日期禁止发送，防止把旧行情当作今日。

`--notify-only` 只发送已经生成且日期为今日的 `output/notification.md`，不重新下载/绘图。云端采用「采集保存 → 选配发布页面 → 提交通知」的顺序，避免新消息指向尚未更新的图表。

云端先写入含运行ID和重跑次数的发送预留并成功提交仓库，然后才调用API；其他运行不能直接消费旧预留。这样即使runner在请求后中断，下一次也会停下核查，避免自动重复发送。当日已成功提交不会重复发送。请求超时等不确定结果会保存为 `uncertain`，不会自动重发；先查微信和Server酱控制台，确认需要后使用 `--retry-push`。API提交成功不等于手机已收到。

`--failure-notice` 只发送运行异常提示，不冒充行情；每天最多提交一次异常提示。主通道本身故障时仍可能无法收到，需要查看GitHub运行日志。

## GitHub 部署（须确认后执行）

### 邮件日报（QQ邮箱 SMTP，单独启用）

邮件包含同一交易日的纯文字、HTML学习解说、内嵌PNG趋势图和公开历史报告链接；不会尝试在邮件中执行交互网页脚本。仅使用Python标准库，无新增大模型API。

1. 在QQ邮箱设置 → 账号与安全 → 安全设置，开启IMAP/SMTP并生成授权码，参见[QQ邮箱官方帮助](https://help.mail.qq.com/detail/106/985)。SMTP通过 `smtp.qq.com:465` 使用证书验证的SSL连接。
2. 将 `SMTP_PASSWORD`（授权码）、`SMTP_USERNAME`（完整发件邮箱）、`MAIL_TO`（单个收件邮箱）保存在GitHub Actions **Secrets**，不要填到公开变量、源码、报告或日志。发件和收件可以相同。
3. 经用户确认后，手动运行工作流并仅勾选 `test_email`，查看SMTP提交结果和实际收件箱/垃圾箱；服务端接受不代表已进入收件箱。需要单独发送最近可用交易日日报时选择 `send_email`，微信的 `send_message` 保持关闭。
4. 验收后将变量 `ENABLE_EMAIL=true`，纳入既有定时任务；`ENABLE_PUSH` 独立控制微信，可同时保留。
5. 两渠道使用独立去重键，邮件为 `email:交易日`，微信保持原交易日键。矩阵任务 `max-parallel:1` 防止共同写推送状态时冲突，`fail-fast:false` 确保某渠道失败不阻止另一个尝试。两个渠道均遵守持久预留 → 提交 → 保存状态；结果不确定或旧预留不会在补漏任务中自动重发。整体流程异常会尝试通过启用的渠道提醒（每渠道、每自然日最多一次）；某渠道成功发送了日报，仍可能收到“另一渠道或其他环节异常”的提醒，不能将提醒理解为所有渠道都失败。

本地验证只用 `--no-push` 或 `--print-report-date`；云端通知专用命令使用 `--channel email` 和来自报告作业输出的 `REPORT_DATE`，不能人工绕过过期日期检查。

1. 在自己的账号创建专用仓库，将项目文件放仓库根目录，包含隐藏的 `.github/`。不要上传 `.venv`、`.env`、个人持仓或临时文件。
2. 在 `Settings → Secrets and variables → Actions` 添加 Secret：`SERVERCHAN_SENDKEY`。
3. 先保持 `ENABLE_PUSH` 未设置。在 Actions 手动运行 `IC IM spread monitor`，`send_message` 保持 false；检查该云端网络是否能访问中金所、报告是否正确、历史是否自动写回。
4. 完成一次已确认的手机测试后，才将 repository variable `ENABLE_PUSH` 设为 `true`，启用定时推送。计划为 `Asia/Shanghai` 周一至周五18:37，节假日由程序判断。GitHub可能延迟执行，不保证准点。
5. 手动运行仍默认不发；需要发送当日日报时显式勾选 `send_message`。`test_connection` 仅发送一次「IC/IM 监控接入测试」，不占用当日日报的去重记录；`refresh_data` 强制访问交易所，用于核验云端网络。并发任务串行，写历史和推送状态不依赖通知成功。
6. Artifact `ic-im-report` 保留30天，可在登录GitHub后下载；历史和抓取快照持续保存在仓库中。

### 从微信免登录打开图表

Server酱只引用公网图片URL，不会自动上传本地图片。未部署页面时，微信可接收文字，完整报告从私有Artifact下载。

本包预留 GitHub Pages：明确同意公开报告、确认账号套餐支持后，在仓库 Pages 中选 GitHub Actions，设置 `ENABLE_PAGES=true` 和 `REPORT_BASE_URL=https://用户名.github.io/仓库名/`。变量未设置时不会发布。

GitHub Free支持公开仓库的Pages，私有仓库的Pages需要支持的付费套餐。私有源码不意味着发布的页面私有。对外只发布 `output/` 的市场行情报告，不含密钥、推送状态或个人持仓；不要把个人账户资料加入输出。

来源：
- https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages
- https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule
- https://sct.ftqq.com/docs/getting-started/faq/

## 日历与数据边界

`trading_calendar.json` 当前覆盖2025、2026年沪深股票市场官方休市表，用于股指交易日和第三个周五的假日顺延；特殊交割调整通过 `expiry_overrides` 指定，须以中金所通知核对。2027年等未覆盖年份的辅助到期日明确属于估算，不能称作精确。

行情优先HTTPS；当前网络可能需要HTTP回退。实际来源和响应哈希保留在快照中，但哈希只用于追溯，不构成传输真实性认证。网站访问控制、字段调整、异常停市仍需维护。交易所数据缺失时不插值、不伪造、不用主力连续价格替代。
