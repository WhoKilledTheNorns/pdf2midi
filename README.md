# Audited Score to MIDI

将清晰的 PDF 或图片乐谱转录为按小节对齐的 Standard MIDI File，并输出可复核的乐谱 IR、来源哈希和独立审计报告。

当前版本线：**v0.1**。仓库的 `main` 分支保存最新已提交版本；需要精确复现时，请记录具体 commit SHA。v0.1 当前尚未发布 Git tag，因此不要使用不存在的 `v0.1.0` 标签进行安装或更新。

## 功能概览

- 保留 BPM、拍号、调号、休止、跨小节连音和末尾空小节；
- 使用精确有理数表示节拍位置，避免浮点累计误差；
- 先生成并验证 `score.json`，再确定性编译 MIDI；
- 独立检查 MIDI 轨道、事件、结束位置以及 IR/来源哈希；
- 对无法可靠识别的反复、弱起、移调、打击乐和含混声部停止猜测并报告阻塞项。

本仓库是一个完整 Codex Skill。不要只复制 `SKILL.md`；`scripts/`、`references/` 和 `agents/` 也是技能的一部分。OpenAI 官方文档也采用“一个技能目录包含 `SKILL.md` 及可选脚本、参考资料和资源”的结构：[Build skills](https://developers.openai.com/plugins/build/skills)。

## 安装前准备

需要：

- Git；
- Python 3.8 或更高版本；
- Codex；
- PDF 证据提取需要 Poppler 的 `pdfinfo` 和 `pdfimages`；
- 页面渲染或无内嵌图片时需要 `pdftoppm`；
- `pdftotext` 用于可选的文本层检测。

确认依赖已进入 `PATH`：

```text
python --version
pdfinfo -v
pdfimages -v
# 页面渲染和文本层检测
pdftoppm -v
pdftotext -v
```

Windows 若没有 `python` 命令，可尝试 `py -3`。本技能的 Python 脚本仅使用标准库；Poppler 负责 PDF 检查、图片提取和页面渲染。

如果目标目录中已经存在本技能，不要再次运行 `git clone`，请直接使用下文的更新命令。

## 用户级安装（推荐）

用户级安装可供不同项目和会话共同使用。OpenAI 官方示例使用 `~/.codex/skills/<skill-name>`；仓库级技能则使用项目内的 `.codex/skills/<skill-name>`：[Testing Agent Skills Systematically](https://developers.openai.com/blog/eval-skills)。

### Windows PowerShell

```powershell
$skillHome = if ($env:CODEX_HOME) {
    $env:CODEX_HOME
} else {
    Join-Path $env:USERPROFILE ".codex"
}
$skillRoot = Join-Path $skillHome "skills"
$skillDir = Join-Path $skillRoot "audited-score-to-midi"

New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
git clone https://github.com/WhoKilledTheNorns/pdf2midi.git $skillDir
```

### macOS / Linux

```bash
skill_root="${CODEX_HOME:-$HOME/.codex}/skills"
skill_dir="$skill_root/audited-score-to-midi"

mkdir -p "$skill_root"
git clone https://github.com/WhoKilledTheNorns/pdf2midi.git "$skill_dir"
```

安装后建议新建 Codex 会话，通过 `/skills` 检查技能，或在提示中显式调用：

```text
Use $audited-score-to-midi to convert this score PDF into a measure-aligned MIDI file with an audit report.
```

## 仓库级安装（可选）

如果只希望某一个 Git 项目使用该技能，可在该项目根目录中把它作为子模块安装：

### Windows PowerShell

```powershell
New-Item -ItemType Directory -Force -Path ".codex\skills" | Out-Null
git submodule add -b main https://github.com/WhoKilledTheNorns/pdf2midi.git .codex/skills/audited-score-to-midi
git commit -m "chore: add audited-score-to-midi skill"
```

### macOS / Linux

```bash
mkdir -p .codex/skills
git submodule add -b main https://github.com/WhoKilledTheNorns/pdf2midi.git .codex/skills/audited-score-to-midi
git commit -m "chore: add audited-score-to-midi skill"
```

子模块方式会把技能版本固定在父仓库记录的 commit，适合需要可复现环境的项目。

## 更新用户级安装

更新前先检查本地修改。下面使用 `--ff-only`，远程历史无法快进时会停止，不会自动创建合并提交。

### Windows PowerShell

```powershell
$skillHome = if ($env:CODEX_HOME) {
    $env:CODEX_HOME
} else {
    Join-Path $env:USERPROFILE ".codex"
}
$skillDir = Join-Path $skillHome "skills\audited-score-to-midi"

git -C $skillDir status --short
git -C $skillDir pull --ff-only origin main
git -C $skillDir log -1 --oneline
git -C $skillDir describe --tags --always
```

### macOS / Linux

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/audited-score-to-midi"

git -C "$skill_dir" status --short
git -C "$skill_dir" pull --ff-only origin main
git -C "$skill_dir" log -1 --oneline
git -C "$skill_dir" describe --tags --always
```

如果 `status --short` 有输出，先审查并保存自己的修改，再更新。不要用强制重置覆盖尚未备份的本地内容。

更新完成后，建议新建 Codex 会话，使技能元数据和说明重新加载。

## 更新仓库级子模块

在父仓库根目录执行：

```text
git submodule update --remote .codex/skills/audited-score-to-midi
git add .codex/skills/audited-score-to-midi
git commit -m "chore: update audited-score-to-midi skill"
```

这会更新父仓库记录的技能 commit。团队其他成员拉取父仓库后可执行：

```text
git submodule update --init --recursive
```

## 更新后验证

进入技能目录，确认入口文件和脚本存在：

```text
SKILL.md
scripts/validate_score_ir.py
scripts/compile_midi.py
scripts/validate_midi.py
```

然后运行：

```text
python scripts/validate_score_ir.py --help
python scripts/compile_midi.py --help
python scripts/validate_midi.py --help
```

Windows 可将 `python` 替换为 `py -3`。更新失败时，先运行 `git status` 和 `git remote -v`，确认当前目录、分支和远程地址，再处理本地修改或网络问题。

## 版本记录

### v0.1

- 单一权威 PDF/图片来源及 SHA-256 绑定；
- 规范化 `score.json` IR；
- BPM 与 metronome beat unit 精确换算；
- Standard MIDI Format 1 确定性编译；
- IR、MIDI、来源和小节终点独立审计；
- 重叠截图段、小节语义与证据清单复核工具。
