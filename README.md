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
8. 安全执行：默认 dry-run，只有 `--apply` 才会落盘。
9. 可回滚：`--apply` 时写入 undo 日志，支持恢复。

## 命令引导

- 直接执行 `simiu`（不带子命令）会显示引导面板。
- 主要子命令：`group`、`undo`。

## 常用命令

- 预览分组：

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
