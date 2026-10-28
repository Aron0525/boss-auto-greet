# 隐私审查记录

| 路径 | 信息类别 | 处理结果 |
| --- | --- | --- |
| 原目录 `.git/**` | 含下列已跟踪文件的历史版本 | 未复制到公开版；公开版建立独立 Git 历史。 |
| 原目录 `config.yaml`, `profile.yaml` | 本地配置、个人档案与经历 | 未复制；公开版只保留虚构筛选示例及空白档案。 |
| 原目录 `contacted.json` | 联系对象、聊天与投递记录 | 未复制；公开版只保留本地记录代码。 |
| 原目录 `run*.log`, `watch*.log` | 运行日志与求职活动 | 未复制。 |
| 原目录 `投递清单*.md`, `投递清单*.html`, `HR沟通消息-三版.md` | 求职、沟通与投递内容 | 未复制。 |
| 原目录 `probe_salary.png`, `shots*/**`, `_*png` | 截图与页面状态 | 未复制。 |
| 原目录 `_probe*`, `tools/_probe*`, `_check*`, `tmp_cap*`, `sent_fixed.json`, `.workbuddy/**` | 一次性探针、临时数据与运行产物 | 未复制。 |
| 原目录 `README.md`, `requirements.txt`, `start_browser.bat`, `tools/run_zhujiang.sh` | 本机路径或个人运行说明 | 未复制原文件；公开版重新编写安装与浏览器说明。 |
| 原目录 `main.py`, `message/**`, `pages/**`, `core/**`, `commands/**` | 功能代码、运行案例注释 | 仅参考功能；公开版独立修订，去除具体个人案例。 |
| 公开版 `config/config.example.yaml`, `bossgreet/examples/config.example.yaml` | 搜索配置示例 | 标为虚构示例；不含个人身份、联系方式或密钥。 |
| 公开版 `config/profile.example.yaml`, `bossgreet/examples/profile.example.yaml` | 个人档案字段 | 内容留空。 |
| 公开版 `bossgreet/messaging/templates.yaml`, `tests/*.py` | 招呼模板、岗位与经历模拟数据 | 改为通用话术或明确虚构数据。 |
| 公开版 `docs/操作手册.md` | 安装与操作示例 | 使用本机环境变量及空白示例，不含个人资料。 |
| 公开版 `bossgreet/core/launcher.py`, `scripts/doctor.py`, `scripts/start_browser.ps1` | 诊断信息、本机浏览器路径 | 诊断不输出账号文字或 Cookie；浏览器配置目录取本机环境变量。 |
| 公开版 Git 暂存文件（57 个） | 联系方式、密钥、令牌、本机绝对路径、运行产物 | 自动复扫未发现命中；人工复核了示例档案与代码注释。 |
