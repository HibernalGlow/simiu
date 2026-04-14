# simiu

`simiu` 是一个用于插画整理的命令行工具。

目标：把同一张图的多个差分自动归到一个分组文件夹里，方便浏览。

## 快速开始

1. 安装依赖并本地可编辑安装：

```bash
pip install -e .
```

2. 先做预览（不会移动文件）：

```bash
simiu group "D:/path/to/artist_pack"
```

3. 确认结果后执行实际操作：

```bash
simiu group "D:/path/to/artist_pack" --apply --mode move
```

## 设计思路

1. 相似库分组：使用 OpenCV 的 pHash 特征做主特征，结合宽高比、颜色均值、文件大小做保守聚类。
2. 不依赖文件名：分组决策完全基于图像特征，不使用文件名规则。
3. 同层隔离：每个文件夹单独计算分组，不跨父子目录混分。
4. 支持遍历：默认递归扫描子目录，且每层只处理本层图片。
5. 扫描顺序：递归时默认 `--scan-order smallest-first`，从最小文件夹开始。
6. 命令框架：统一使用 Typer，入口命令和参数更清晰。
6. 路径输入优化：支持命令参数、剪贴板路径、交互式输入（参考 psdc 体验）。
7. 显示美化：使用 `rich` 输出分组预览表格和执行摘要面板。
8. 安全执行：默认 dry-run，预览后会询问是否立即 apply（默认 yes）。
9. 可回滚：`--apply` 时写入 undo 日志，支持恢复。
10. 配置支持：可通过 `simiu.toml` 自定义分组目录前缀。
11. 性能优化：默认启用特征提取并行与比对剪枝，可在配置中调节并行度。
12. 去重处理：默认跳过以 `name_prefix` 开头的目录（视为已处理，避免重复嵌套）。

## 命令引导

- 直接执行 `simiu`（不带子命令）会显示引导面板。
- 主要子命令：`group`、`undo`。

## 常用命令

- 预览分组：

```bash
simiu group "D:/pack"
```

- 预览后立即执行（默认 yes）：

```bash
simiu group "D:/pack"
```

- 递归遍历（同层处理，最小目录优先）：

```bash
simiu group "D:/pack" --recursive --scan-order smallest-first
```

- 只处理当前目录（关闭递归）：

```bash
simiu group "D:/pack" --no-recursive
```

- 指定配置文件：

```bash
simiu group "D:/pack" --config "D:/path/to/simiu.toml"
```

## 配置文件

可在以下位置放置配置文件（按优先顺序读取第一个存在的）：

1. `--config` 指定路径
2. 当前工作目录 `simiu.toml`
3. 目标目录 `simiu.toml`
4. 目标目录 `.simiu.toml`

示例见 `simiu.toml.example`。

可选性能配置：

```toml
[performance]
max_workers = 0
```

- `0` 表示自动选择并行线程。
- 大目录可手动设为 `8`、`12` 等以进一步提速。

- 从剪贴板读取路径：

```bash
simiu group --clipboard
```

- 不传路径，进入交互输入：

```bash
simiu group
```

- 实际移动：

```bash
simiu group "D:/pack" --apply --mode move
```

- 复制不移动：

```bash
simiu group "D:/pack" --apply --mode copy
```

- 撤销上次操作：

```bash
simiu undo "D:/pack/.simiu-undo-20260414-120000.json"
```

## gifu（压缩包转动图）

按压缩包内部文件顺序读取图片，批量转换为 gif/webp/apng。

- 无参数直接进入交互引导：

```bash
gifu
```

- 直接传多个压缩包：

```bash
gifu make "D:/a.zip" "D:/b.cbz"
```

- 输入目录并递归查找压缩包：

```bash
gifu make "D:/packs" --recursive --format webp
```

- 从路径清单批量输入（每行一个，支持注释行 #）：

```bash
gifu make --list-file "D:/archive_list.txt" --format apng
```

- 使用配置文件设置默认格式/质量/命名：

```bash
gifu make "D:/packs" --config "D:/path/to/gifu.toml"
```

默认会给输出文件名加前缀 `[#dyna]`，例如 `a.zip -> [#dyna]a.webp`。

`gifu.toml` 示例：

```toml
[output]
format = "webp"
quality = 85
duration_ms = 120
loop = 0

[naming]
prefix = "[#dyna]"
template = "{prefix}{stem}"

[performance]
max_workers = 0
```

支持模板变量：`{prefix}`、`{stem}`、`{archive}`、`{parent}`。
`max_workers` 为并行转换线程数，`0` 表示自动。
`duration_ms` 和 `loop` 可作为默认动画参数（命令行 `--duration` / `--loop` 可覆盖）。
