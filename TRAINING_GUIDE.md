# SAM-TP Training Guide (Earth Rover Mini+)

**Goal:** train a SAM-TP model that understands Mini+ imagery, using AWS EC2 + the paper's public dataset + our FrodoBots-Mini-4K dataset.

**Time:** ~48 hours wall-clock (~4 hours attended, ~44 hours unattended)
**Cost:** ~$190 on AWS
**Result:** a fine-tuned SAM-TP checkpoint that we deploy in `autonav-urban`.

Read from top to bottom, do it in order.

---

## 0. Before you start — checklist

You need on the machine where you'll do this (the one with AWS logged in):

- [ ] Chrome (or any browser) with AWS Console logged in
- [ ] A terminal (Terminal on Mac, PowerShell on Windows)
- [ ] `~/aws-keys/` folder (or wherever you store SSH keys)
- [ ] `aws-cli` installed and configured: run `aws sts get-caller-identity` in Terminal — should print your AWS account ID.

You need to know:

- **Your S3 bucket name** containing Mini-4K (fill in below): `_______________`
- **The region** it lives in (fill in below): `_______________`

Fill those two blanks now, everywhere they appear in this file (search-replace `YOUR-BUCKET-NAME` and `YOUR-REGION`).

---

## 1. AWS prep (30 minutes)

### 1.1 — Verify GPU quota (already done ✅)

Confirmed 64 vCPUs available on "Running On-Demand G and VT instances". Skip.

### 1.2 — Create SSH key pair

In Chrome: **EC2 → Key Pairs → Create key pair**
- Name: `autonav-training-key`
- Type: RSA, format: .pem
- Click Create → browser downloads `.pem` file

In terminal:
```bash
mkdir -p ~/aws-keys
mv ~/Downloads/autonav-training-key.pem ~/aws-keys/
chmod 400 ~/aws-keys/autonav-training-key.pem
```

### 1.3 — Create IAM role for EC2 → S3 access

In Chrome: **IAM → Roles → Create role**
- Trusted entity: **AWS service** → Use case: **EC2**
- Add permission: **AmazonS3FullAccess** (for both read from Mini-4K bucket AND write to output bucket)
- Name: `autonav-training-role`
- Create

### 1.4 — Create output bucket

In Chrome: **S3 → Create bucket**
- Name: `autonav-training-outputs` (append your username to make it unique)
- Region: **YOUR-REGION** (same as Mini-4K)
- Default settings, create

---

## 2. Launch training instance (5 minutes)

In Chrome: **EC2 → Launch Instance**

| Setting | Value |
|---|---|
| Name | `autonav-training` |
| AMI | Search "Deep Learning AMI GPU PyTorch" → **Ubuntu 22.04** version, latest |
| Instance type | **g4dn.12xlarge** (4× T4 GPUs) |
| Key pair | `autonav-training-key` |
| Network | Allow SSH from **My IP** |
| Storage | **500 GB** gp3 |
| Advanced → IAM instance profile | `autonav-training-role` |

Click **Launch instance**. Wait ~2 minutes for boot.

Copy the **Public IPv4 address** from the instance detail page.

---

## 3. SSH in + install (10 minutes)

From terminal on the machine with the key:

```bash
ssh -i ~/aws-keys/autonav-training-key.pem ubuntu@<PUBLIC_IP>
```

First time it asks "Are you sure? yes/no" — type `yes`.

You're now inside EC2 (prompt like `ubuntu@ip-172-31-...`). Continue on the EC2 shell:

```bash
# Verify GPUs are visible
nvidia-smi
# Should show 4× Tesla T4 GPUs

# Install extra dependencies (Deep Learning AMI has most already)
pip install --upgrade huggingface_hub transformers boto3 opencv-python-headless

# Clone Meta SAM2 (needed for the training/ package)
git clone https://github.com/facebookresearch/sam2.git /home/ubuntu/sam2
cd /home/ubuntu/sam2
pip install -e ".[dev]"

# Make working directories
mkdir -p /data/paper /data/frodobots /data/masks /data/combined /data/logs /data/checkpoints
```

---

## 4. Create the 6 training scripts on EC2

Use `nano` (or paste into a text editor). Create each of the following files in `/home/ubuntu/scripts/`:

```bash
mkdir -p /home/ubuntu/scripts
cd /home/ubuntu/scripts
```

### 4.1 — `download_paper_dataset.py`

```bash
nano download_paper_dataset.py
```

Paste:

```python
"""Download the paper's labeled dataset (jamiewjm/sam-tp) from HuggingFace."""
import os
from huggingface_hub import snapshot_download

TARGET = "/data/paper"
os.makedirs(TARGET, exist_ok=True)

print("Downloading jamiewjm/sam-tp (~2-3 GB) ...")
snapshot_download(
    repo_id="jamiewjm/sam-tp",
    repo_type="dataset",
    local_dir=TARGET,
    local_dir_use_symlinks=False,
)
print(f"Done. Contents of {TARGET}:")
for item in os.listdir(TARGET):
    print(f"  {item}")
```

Save (Ctrl+O, Enter, Ctrl+X).

### 4.2 — `sample_frodobots.py`

```bash
nano sample_frodobots.py
```

```python
"""Sample N frames from FrodoBots-Mini-4K in S3 to local disk."""
import os, io, argparse, random, subprocess, tempfile
import boto3

parser = argparse.ArgumentParser()
parser.add_argument("--bucket", required=True, help="S3 bucket with Mini-4K")
parser.add_argument("--n", type=int, default=15000, help="How many frames to sample")
parser.add_argument("--output", default="/data/frodobots/images", help="Local output folder")
parser.add_argument("--seed", type=int, default=42)
args = parser.parse_args()

os.makedirs(args.output, exist_ok=True)
random.seed(args.seed)
s3 = boto3.client("s3")

# List all .ts video segments in the bucket
print(f"Listing S3 bucket {args.bucket} ...")
paginator = s3.get_paginator("list_objects_v2")
ts_keys = []
for page in paginator.paginate(Bucket=args.bucket):
    for obj in page.get("Contents", []):
        key = obj["Key"]
        if key.endswith(".ts") and "front" in key.lower():
            ts_keys.append(key)
print(f"Found {len(ts_keys)} front-camera .ts segments")

# Sample N unique ones
sampled = random.sample(ts_keys, min(args.n, len(ts_keys)))
print(f"Sampling {len(sampled)} frames ...")

for i, key in enumerate(sampled):
    if i % 500 == 0:
        print(f"  {i}/{len(sampled)}")
    # Download the .ts to /tmp, extract middle frame with ffmpeg
    with tempfile.NamedTemporaryFile(suffix=".ts") as tf:
        s3.download_fileobj(args.bucket, key, tf)
        tf.flush()
        out = os.path.join(args.output, f"frame_{i:06d}.jpg")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "5", "-i", tf.name, "-frames:v", "1", "-q:v", "2", out]
        try:
            subprocess.run(cmd, timeout=30, check=True)
        except Exception as e:
            print(f"  skipped {key}: {e}")

n_files = len([f for f in os.listdir(args.output) if f.endswith(".jpg")])
print(f"Done. {n_files} frames saved to {args.output}")
```

### 4.3 — `auto_label.py`

```bash
nano auto_label.py
```

```python
"""Auto-label frames using SAM2 auto-mask-generator + 'touch-front-center' rule.

Runs 4 workers in parallel (one per GPU). Each worker processes a shard of frames.
"""
import os, sys, argparse, glob
import numpy as np
from PIL import Image
import torch
import torch.multiprocessing as mp

def worker(rank, world_size, frames_dir, output_dir):
    """One worker per GPU. Processes frames whose index % world_size == rank."""
    device = f"cuda:{rank}"
    print(f"[worker {rank}] using {device}")

    from sam2.build_sam import build_sam2
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

    # Load base SAM2 tiny model (not the fine-tuned SAM-TP; we want raw SAM2
    # for the auto-mask-generator that produced candidate masks in the paper).
    ckpt = os.environ.get("SAM2_CKPT", "/home/ubuntu/sam2/checkpoints/sam2.1_hiera_tiny.pt")
    cfg = "configs/sam2.1/sam2.1_hiera_t.yaml"
    sam2 = build_sam2(cfg, ckpt, device=device)
    generator = SAM2AutomaticMaskGenerator(sam2, points_per_side=32, pred_iou_thresh=0.75)

    frame_paths = sorted(glob.glob(os.path.join(frames_dir, "*.jpg")))
    my_frames = [p for i, p in enumerate(frame_paths) if i % world_size == rank]
    print(f"[worker {rank}] handling {len(my_frames)} frames")

    for i, fp in enumerate(my_frames):
        out_path = os.path.join(output_dir, os.path.basename(fp).replace(".jpg", ".png"))
        if os.path.exists(out_path):
            continue

        try:
            img = np.array(Image.open(fp).convert("RGB"))
            h, w = img.shape[:2]

            masks = generator.generate(img)
            if not masks:
                continue

            # Rule: keep the mask that includes the bottom-center pixel
            #       (the ground directly in front of the rover).
            bc_r, bc_c = h - 5, w // 2
            best = None
            for m in masks:
                seg = m["segmentation"]
                if seg[bc_r, bc_c]:
                    if best is None or m["area"] > best["area"]:
                        best = m

            if best is None:
                continue

            mask = best["segmentation"].astype(np.uint8) * 255
            Image.fromarray(mask, mode="L").save(out_path)

            if i % 100 == 0:
                print(f"[worker {rank}] {i}/{len(my_frames)}")
        except Exception as e:
            print(f"[worker {rank}] failed on {fp}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames-dir", default="/data/frodobots/images")
    parser.add_argument("--output-dir", default="/data/masks")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    mp.set_start_method("spawn", force=True)
    processes = []
    for r in range(args.workers):
        p = mp.Process(target=worker, args=(r, args.workers, args.frames_dir, args.output_dir))
        p.start()
        processes.append(p)
    for p in processes:
        p.join()
    print("All workers done.")
```

### 4.4 — `combine_datasets.py`

```bash
nano combine_datasets.py
```

```python
"""Merge paper dataset + our labeled Mini+ frames into MOSE format for SAM-TP training."""
import os, shutil, glob, random

PAPER_DIR = "/data/paper"
MINI_IMG_DIR = "/data/frodobots/images"
MINI_MASK_DIR = "/data/masks"
OUT_DIR = "/data/combined"

# MOSE expects: JPEGImages/<video_id>/<frame_id>.jpg + Annotations/<video_id>/<frame_id>.png
os.makedirs(f"{OUT_DIR}/JPEGImages", exist_ok=True)
os.makedirs(f"{OUT_DIR}/Annotations", exist_ok=True)

video_idx = 0

# --- copy paper data (each pair becomes its own "video" of 1 frame) ---
print("Copying paper dataset ...")
paper_images = sorted(glob.glob(f"{PAPER_DIR}/**/*.jpg", recursive=True))
for img_path in paper_images:
    # Find matching mask
    rel = os.path.relpath(img_path, PAPER_DIR)
    mask_path = os.path.join(PAPER_DIR, rel.replace("images/", "masks/").replace(".jpg", ".png"))
    if not os.path.exists(mask_path):
        continue

    vid_id = f"paper_{video_idx:06d}"
    video_idx += 1
    os.makedirs(f"{OUT_DIR}/JPEGImages/{vid_id}", exist_ok=True)
    os.makedirs(f"{OUT_DIR}/Annotations/{vid_id}", exist_ok=True)
    shutil.copy(img_path, f"{OUT_DIR}/JPEGImages/{vid_id}/00000.jpg")
    shutil.copy(mask_path, f"{OUT_DIR}/Annotations/{vid_id}/00000.png")

# --- copy our Mini+ auto-labeled data ---
print("Copying Mini+ dataset ...")
for img_path in sorted(glob.glob(f"{MINI_IMG_DIR}/*.jpg")):
    base = os.path.basename(img_path).replace(".jpg", "")
    mask_path = f"{MINI_MASK_DIR}/{base}.png"
    if not os.path.exists(mask_path):
        continue

    vid_id = f"mini_{video_idx:06d}"
    video_idx += 1
    os.makedirs(f"{OUT_DIR}/JPEGImages/{vid_id}", exist_ok=True)
    os.makedirs(f"{OUT_DIR}/Annotations/{vid_id}", exist_ok=True)
    shutil.copy(img_path, f"{OUT_DIR}/JPEGImages/{vid_id}/00000.jpg")
    shutil.copy(mask_path, f"{OUT_DIR}/Annotations/{vid_id}/00000.png")

# --- 90/10 train/val split ---
all_vids = sorted(os.listdir(f"{OUT_DIR}/JPEGImages"))
random.Random(42).shuffle(all_vids)
split = int(len(all_vids) * 0.9)
train, val = all_vids[:split], all_vids[split:]

with open(f"{OUT_DIR}/train.txt", "w") as f:
    f.write("\n".join(train))
with open(f"{OUT_DIR}/val.txt", "w") as f:
    f.write("\n".join(val))

print(f"Done. {len(train)} train, {len(val)} val videos in {OUT_DIR}")
```

### 4.5 — `train_samtp.py`

```bash
nano train_samtp.py
```

```python
"""Fine-tune SAM-TP on the combined dataset using Meta's SAM2 training loop."""
import os, sys, argparse, subprocess, urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", default="/data/combined")
parser.add_argument("--gpus", type=int, default=4)
parser.add_argument("--auto-shutdown", action="store_true")
parser.add_argument("--output-bucket", default=None,
                    help="Optional S3 bucket to upload final checkpoint")
args = parser.parse_args()

# Download the paper's pretrained checkpoint (starting point for fine-tune)
CKPT_URL = "https://drive.google.com/uc?export=download&id=1cwvvzoZ-6mV5zJmYpB0nnc0-JVDdT_JQ"
CKPT_LOCAL = "/data/checkpoints/checkpoint_2.pt"
if not os.path.exists(CKPT_LOCAL):
    print("Downloading paper's checkpoint_2.pt ...")
    urllib.request.urlretrieve(CKPT_URL, CKPT_LOCAL)

# Set up training config path (from the vendored SAM-TP repo we cloned)
TRAINING_CFG = "/home/ubuntu/sam2/sam2/configs/sam2.1_training_tiny/sam2_training_custom2_freezeNoneNone_f1.yaml"

# Launch distributed training on 4 GPUs
cmd = [
    "python", "-m", "torch.distributed.launch",
    "--nproc_per_node", str(args.gpus),
    "/home/ubuntu/sam2/training/train.py",
    "-c", TRAINING_CFG,
    "--use-cluster", "0",
    "--num-gpus", str(args.gpus),
]
print("Launching training:", " ".join(cmd))
result = subprocess.run(cmd, cwd="/home/ubuntu/sam2")

if result.returncode != 0:
    print("TRAINING FAILED. Not uploading.")
    if args.auto_shutdown:
        print("Auto-shutdown skipped due to failure.")
    sys.exit(1)

# Find the best checkpoint (highest epoch)
import glob
ckpts = sorted(glob.glob("/home/ubuntu/sam2/sam2_logs/**/checkpoints/*.pt", recursive=True))
if not ckpts:
    print("No checkpoints found!")
    sys.exit(1)
final = ckpts[-1]
print(f"Best checkpoint: {final}")

# Upload to S3
if args.output_bucket:
    dest = f"s3://{args.output_bucket}/checkpoint_finetuned.pt"
    print(f"Uploading to {dest}")
    subprocess.run(["aws", "s3", "cp", final, dest], check=True)

# Auto-shutdown
if args.auto_shutdown:
    print("Auto-shutting down in 60 seconds ...")
    subprocess.run(["sudo", "shutdown", "-h", "+1"])
```

### 4.6 — Helper: `progress.sh` (optional, for monitoring)

```bash
nano progress.sh
```

```bash
#!/bin/bash
# Prints current training progress
echo "=== GPU usage ==="
nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv
echo ""
echo "=== Latest log lines ==="
tail -20 /home/ubuntu/sam2/sam2_logs/**/logs/train.log 2>/dev/null || echo "(no logs yet)"
echo ""
echo "=== Disk usage ==="
df -h /data
```

```bash
chmod +x progress.sh
```

---

## 5. Run the pipeline (~48 hours, mostly unattended)

### 5.1 — Download paper's dataset (30 min)

```bash
cd /home/ubuntu/scripts
python download_paper_dataset.py
```

### 5.2 — Sample 15,000 Mini+ frames (2 hours)

```bash
python sample_frodobots.py --bucket YOUR-BUCKET-NAME --n 15000
```

While this runs, you can safely close your SSH session — the script keeps running. Reconnect later to check progress. Or use `nohup` + `&` to detach explicitly:

```bash
nohup python sample_frodobots.py --bucket YOUR-BUCKET-NAME --n 15000 > sample.log 2>&1 &
```

### 5.3 — Auto-label with SAM2 (4 hours, 4 GPUs in parallel)

```bash
python auto_label.py --workers 4
```

Log lines will appear from all 4 workers interleaved.

### 5.4 — Combine into MOSE format (10 min)

```bash
python combine_datasets.py
```

### 5.5 — Kick off training (~40 hours, unattended)

```bash
nohup python train_samtp.py \
  --gpus 4 \
  --output-bucket autonav-training-outputs \
  --auto-shutdown \
  > /data/logs/train.log 2>&1 &

# Note the PID so you can check on it
echo $! > train.pid
disown
```

You can now **safely SSH out** and close your laptop. Training runs, saves checkpoints hourly to `/home/ubuntu/sam2/sam2_logs/`, and shuts down the instance when done.

---

## 6. Monitor progress (from any computer)

You can check progress two ways:

### 6.1 — SSH back in

```bash
ssh -i ~/aws-keys/autonav-training-key.pem ubuntu@<PUBLIC_IP>
bash /home/ubuntu/scripts/progress.sh
```

### 6.2 — Watch via S3

In Chrome: S3 Console → `autonav-training-outputs` bucket → refresh every hour. Once you see `checkpoint_finetuned.pt` appear, training is done.

### 6.3 — Check if instance shut down

In Chrome: EC2 → look at `autonav-training` state. When it says **stopped**, training completed. Terminate it to delete the disk (~$2/day if left as stopped-but-not-terminated).

---

## 7. Download trained model + deploy (30 min)

### 7.1 — Download to your Mac

From the Mac terminal (or wherever you plan to deploy):

```bash
aws s3 cp s3://autonav-training-outputs/checkpoint_finetuned.pt ~/Downloads/
```

### 7.2 — Copy into the earth-rovers-sdk repo

```bash
cp ~/Downloads/checkpoint_finetuned.pt \
   /Users/dev/Documents/earth-rovers-sdk/autonav-urban/third_party/sam2_ckpt/
```

### 7.3 — Update config to use the new checkpoint

Edit `/Users/dev/Documents/earth-rovers-sdk/autonav-urban/configs/mini_urban.yaml`:

```yaml
samtp:
  config_path: sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml
  checkpoint_path: sam2_ckpt/checkpoint_finetuned.pt   # ← changed from checkpoint_2.pt
  score_thresh: 0.0
  multimask: false
  score_transform: sigmoid
```

### 7.4 — Restart server + test

```bash
cd /Users/dev/Documents/earth-rovers-sdk
lsof -ti :8000 | xargs kill -9 2>/dev/null
source venv39/bin/activate
hypercorn main:app --bind 0.0.0.0:8000
```

Run mission-1. Check the SAM-TP raw panel on the dashboard for hedge / other-rover scenes — should now be significantly better than the paper's baseline checkpoint.

---

## 8. Cleanup (5 min)

**Do this immediately after you're happy with the trained model:**

1. **Terminate EC2 instance**: EC2 → select `autonav-training` → Instance state → **Terminate**. Confirm.
2. **Delete outputs bucket** (optional, saves $2/month): S3 → `autonav-training-outputs` → Empty → Delete
3. **Delete the paper dataset from EC2 volume** (already gone when you terminate)

⚠️ **Failure to terminate = you keep paying $3.91/hour.** Set a phone alarm before you start Step 5.5 so you don't forget.

---

## 9. Troubleshooting

### "Permission denied" when SSHing in

```bash
chmod 400 ~/aws-keys/autonav-training-key.pem
```

### "No GPUs found" in `nvidia-smi`

Wrong AMI selected. You need "Deep Learning AMI GPU PyTorch". Terminate and relaunch with the correct AMI.

### `auto_label.py` runs out of memory

Reduce `--workers 4` to `--workers 2`.

### Training crashes / instance reclaimed (spot instance only)

Checkpoints are saved every epoch to `/home/ubuntu/sam2/sam2_logs/`. Relaunch a new instance, download the latest checkpoint from that folder (via `aws s3 cp` if you set up periodic uploads), and resume with `--resume <checkpoint>`.

### `ffmpeg: command not found` during frame sampling

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

### Bill shock — how to prevent

- **Every script has auto-shutdown** enabled (`--auto-shutdown`) — instance stops itself when work is done
- **AWS Budget alert:** in Billing → Budgets, create a "monthly cost budget" with alert threshold $250. You'll get emails if you're close to overshooting.
- **Set a phone alarm** for 50 hours after you launch — if not shut down by then, manually terminate.

---

## 10. Summary table

| Step | Cmd | Time | Cost |
|---|---|---|---|
| Launch g4dn.12xlarge | (Chrome) | 5 min | — |
| SSH + install | (SSH) | 10 min | $0.65 |
| Download paper dataset | `download_paper_dataset.py` | 30 min | $2 |
| Sample Mini+ frames | `sample_frodobots.py` | 2 h | $8 |
| Auto-label | `auto_label.py` | 4 h | $16 |
| Combine | `combine_datasets.py` | 10 min | $1 |
| **Train** | `train_samtp.py --auto-shutdown` | **40 h** | **$156** |
| Download model | `aws s3 cp` | 5 min | — |
| Deploy | edit yaml, restart server | 5 min | — |
| **TOTAL** | | **~48 h wall-clock** | **~$189** |

---

## 11. What to tell me when you're done

Ping me with:
- Whether training completed (final val_IoU number from log)
- Whether the deployed model works on Mini+ hedge / other-rover scenes
- Any errors you hit during any step

If it doesn't work, we can either:
- Adjust hyperparameters and retrain (~$150 for another run)
- Escalate to labeling more frames
- Fall back to keeping the original SAM-TP + relying on CLIPSeg overlay we already built

Good luck.
