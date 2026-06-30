# Temporal-Caption Bench 标注工具

用于核验 **Temporal-Caption Bench**（第一批，150 个同 query 组 / 416 个片段切片）的本地浏览器标注工具。
代码托管在 GitHub，数据从 HuggingFace 拉取（[`XinNUS/Temporal_Caption_Bench`](https://huggingface.co/datasets/XinNUS/Temporal_Caption_Bench)）。
每位标注者在自己机器上启动服务，只看自己分到的 50 个组。

## 你要标注什么

每个**组（group）** = 一个视频 + 一个共享的 grounding **query**，这个 query 在该视频的 K 个片段里都出现。
每个片段你会看到一个切片，外加 Gemini 预标的 `facts`（分成 ★specific / ·shared）和 `negatives`，你来核验它们：

- **任务 ①** 逐片段：query 是否真的出现（`query_occurs`）；对整组：各片段之间是否互相**可区分（distinct）**；然后**保留 / 拒绝（keep / reject）**该组。
- **任务 ②** 逐片段：facts / specific 标签 / negatives 是否正确；点击任意有问题的 fact 或 negative 来标记它。

## 安装（每位协作者，只需一次）

```bash
git clone https://github.com/devinxzhang/temporal-caption-bench-annotator.git
cd temporal-caption-bench-annotator
pip install -r requirements.txt

# 如果数据集是私有/受限的，需要先登录 HuggingFace（公开则跳过）：
#   huggingface-cli login

python prepare_data.py        # 下载切片 + 重建 manifest/assignments（约 120 MB）
```

## 运行

```bash
python tcb_server.py --port 8000
# 浏览器打开 http://localhost:8000/ ，用自己的名字登录（zx / whc / lbb）
```

你的修改会自动保存到 `annotations/<你的名字>.json`（原子写入，支持单步撤销）。你只会看到自己的 50 个组。

## 回传结果

`annotations/<你的名字>.json` 已被 **git 忽略**——不要 commit 它。标完后，把这**一个文件**发回给维护者
（或按要求 push 到 `results/` 分支）。重新运行 `prepare_data.py` **不会**覆盖你的标注。

## 文件说明

| 文件 | 作用 |
|---|---|
| `tcb_server.py` | FastAPI 服务：提供 UI、流式传输切片（支持 HTTP Range）、持久化标注 |
| `tcb_review.html` | 单页标注界面 |
| `prepare_data.py` | 从 HF 下载数据集 → 重建 `manifest.json` + `assignments.json` |
| `requirements.txt` | fastapi / uvicorn / huggingface_hub |

## 常见问题

- **下载很慢 / 连不上 HF？** 设置镜像后重试：`export HF_ENDPOINT=https://hf-mirror.com && python prepare_data.py`
- **端口被占用？** 换端口：`python tcb_server.py --port 8123`
- **登录后看不到组？** 确认登录名是 `zx` / `whc` / `lbb` 之一（分配是按名字的）。
- **想重新下载数据？** 删掉 `data/ manifest.json assignments.json` 再跑一次 `prepare_data.py`（标注文件不受影响）。
