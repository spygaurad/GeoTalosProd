# Colab naive-baseline benchmark

Runs the **exact same naive workflow** as the HPC rows (same weights, same
`1024²` tiling, same params) on a free/accessible Colab GPU, so the Colab column
of `tab:compute-comparison` is measured rather than argued.

**Expected headline result:** task1/task2 run (slowly) on Colab; **task3 (SAM3,
4.67 GB scene) OOMs** — the naive script needs 60–101 GB RAM, Colab gives ~12–51 GB.
That OOM *is* the datapoint: the baseline cannot run the large workflow on
accessible hardware, while GeoTALOS does it in 0.63 GB.

---

## 0. Files to transfer

| File | Size | Needed for |
|---|---|---|
| `sam3.pt` | 3.3 GB | task3 |
| `bpe_simple_vocab_16e6.txt.gz` | 1.3 MB | task3 |
| `yolo11x-ortho.pt` | 110 MB | task2 |
| `yolov11x-pose.pt` | 113 MB | task1 |
| `FCAT1_cog.tif` | 307 MB | task1 |
| `JAMACOAQUE6_cog.tif` | 148 MB | task2 |
| `Kotsimba_corrected_cog.tif` | 4.4 GB | task3 |

**Minimal OOM demo (task3 only):** `sam3.pt` + `bpe…gz` + `Kotsimba…tif` ≈ 7.7 GB.

---

## 1. Copy models + data to Google Drive (one-time, from lovelace)

Colab can't reach lovelace directly, so stage through Google Drive.

### 1a. Bundle the files
```bash
mkdir -p ~/colab_bench/models ~/colab_bench/data
cp /home/prass25/projects/GreenMark/models/{sam3.pt,bpe_simple_vocab_16e6.txt.gz,yolo11x-ortho.pt,yolov11x-pose.pt} ~/colab_bench/models/
cp /home/prass25/projects/AwakeForest/datasets/data/dataset_benchmark_cog/{FCAT1_cog.tif,JAMACOAQUE6_cog.tif,Kotsimba_corrected_cog.tif} ~/colab_bench/data/
du -sh ~/colab_bench/*        # ~3.5 GB models, ~4.9 GB data
```

### 1b. Push to Drive — option A: rclone (no sudo)
```bash
# install rclone into ~/bin (no root)
mkdir -p ~/bin && cd /tmp && curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip \
  && unzip -o rclone-current-linux-amd64.zip && cp rclone-*-linux-amd64/rclone ~/bin/ && chmod +x ~/bin/rclone

# headless Drive auth: run `rclone authorize "drive"` on your LAPTOP (has a browser),
# paste the token when `rclone config` on lovelace asks for it. Name the remote `gdrive`.
~/bin/rclone config       # one-time: new remote -> drive -> headless token

# upload (resumable)
~/bin/rclone copy ~/colab_bench gdrive:colab_bench -P
```

### 1b. Push to Drive — option B: manual (no rclone)
```bash
# copy to your laptop, then drag-drop into Google Drive (folder "colab_bench")
# from your LAPTOP:
scp -r prass25@lovelace.deac.wfu.edu:~/colab_bench ./colab_bench
# then upload ./colab_bench to https://drive.google.com  (My Drive/colab_bench)
```

---

## 2. Colab — mount Drive and copy to local disk

> Runtime → Change runtime type → **GPU** (T4/L4). Then:

```python
# Cell 1 — mount + stage to fast local disk (/content)
from google.colab import drive
drive.mount('/content/drive')

!mkdir -p /content/models /content/data
!cp /content/drive/MyDrive/colab_bench/models/* /content/models/
!cp /content/drive/MyDrive/colab_bench/data/*    /content/data/
!ls -lh /content/models /content/data
!nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
!free -h | head -2          # note Colab RAM ceiling (~12.7 GB free / ~25-51 GB Pro)
```

```python
# Cell 2 — deps (match palm_api: ultralytics 8.3.237, torch is preinstalled on Colab)
!pip -q install "ultralytics==8.3.237" rasterio opencv-python-headless pycocotools
import torch; print("torch", torch.__version__, "cuda", torch.cuda.is_available())
```

---

## 3. Colab — the naive benchmark (self-contained)

```python
# Cell 3 — naive runner: same weights, same 1024 tiling, same params as HPC rows
import os, time, json, resource, subprocess, tempfile
import numpy as np, cv2, torch, torchvision, rasterio
from rasterio.windows import Window
from rasterio.warp import transform as warp_transform

MODELS = "/content/models"; DATA = "/content/data"
PATCH = 1024; CONF = 0.25; IOU = 0.7; IMGSZ = 800; MAX_PATCHES = 5000

TASKS = {
  "task1": dict(file="FCAT1_cog.tif",      kind="crown", prompts=None),
  "task2": dict(file="JAMACOAQUE6_cog.tif", kind="yolo",  prompts=None),
  "task3": dict(file="Kotsimba_corrected_cog.tif", kind="sam3",
                prompts=[["crops","farm crops"],["water","pond"],["rooftop","building"]]),
}

_models = {}
def get_yolo():
    from ultralytics import YOLO
    return _models.setdefault("yolo", YOLO(f"{MODELS}/yolo11x-ortho.pt"))
def get_pose():
    from ultralytics import YOLO
    return _models.setdefault("pose", YOLO(f"{MODELS}/yolov11x-pose.pt"))
def get_sam3():
    from ultralytics.models.sam import SAM3SemanticPredictor
    if "sam3" not in _models:
        ov = dict(conf=CONF, task="segment", mode="predict",
                  model=f"{MODELS}/sam3.pt", half=True, verbose=False)
        _models["sam3"] = SAM3SemanticPredictor(overrides=ov,
                              bpe_path=f"{MODELS}/bpe_simple_vocab_16e6.txt.gz")
    return _models["sam3"]

def nms(dets, iou):
    if not dets: return []
    b = torch.tensor([[d['cx']-d['w']/2,d['cy']-d['h']/2,d['cx']+d['w']/2,d['cy']+d['h']/2] for d in dets])
    s = torch.tensor([d['conf'] for d in dets])
    return [dets[i] for i in torchvision.ops.nms(b, s, iou).numpy()]

def mask_polys(binary, min_area=4.0):
    cs,hi = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not cs or hi is None: return []
    hi=hi[0]; out=[]
    for i,h in enumerate(hi):
        if int(h[3])!=-1: continue
        if cv2.contourArea(cs[i])<min_area: continue
        p=cs[i].squeeze(1)
        if p.ndim!=2 or len(p)<3: continue
        out.append([[float(x),float(y)] for x,y in p])
    return out

def png(patch):
    fd,p=tempfile.mkstemp(suffix=".png"); os.close(fd)
    cv2.imwrite(p, cv2.cvtColor(patch, cv2.COLOR_RGB2BGR)); return p

def run_yolo(patch, pose=False):
    m = get_pose() if pose else get_yolo(); t=png(patch)
    try:
        res=m.predict(t,save=False,imgsz=IMGSZ,conf=CONF,iou=IOU,verbose=False)
        dets=[]
        for r in res:
            if r.boxes is None: continue
            for i,b in enumerate(r.boxes.xyxy):
                x1,y1,x2,y2=map(float,b.tolist()[:4])
                kp=None
                if pose and r.keypoints is not None:
                    kd=r.keypoints.data.cpu().numpy(); kp=kd[i] if i<len(kd) else None
                pt=[(x1+x2)/2,(y1+y2)/2]
                if kp is not None:
                    best=0
                    for k in kp:
                        if k[2]>best and (k[0]>0 or k[1]>0): best=k[2]; pt=[float(k[0]),float(k[1])]
                dets.append(dict(cx=(x1+x2)/2,cy=(y1+y2)/2,w=x2-x1,h=y2-y1,
                                 conf=float(r.boxes.conf[i]), xyxy=[x1,y1,x2,y2], pt=pt))
        out=[]
        for d in nms(dets,IOU):
            x1,y1,x2,y2=d['xyxy']
            out.append(dict(point=d['pt']) if pose else dict(poly=[[x1,y1],[x2,y1],[x2,y2],[x1,y2]]))
        return out
    finally:
        os.path.exists(t) and os.unlink(t)

def run_sam3(patch, prompts):
    pr=get_sam3(); t=png(patch)
    try:
        res=pr(source=t, save=False, text=prompts)
        if not res: return []
        r=res[0]
        if r.boxes is None or r.masks is None: return []
        md=r.masks.data.cpu().numpy() if r.masks.data is not None else None
        oh,ow=int(r.orig_shape[0]),int(r.orig_shape[1]); out=[]
        for i in range(len(r.boxes)):
            if md is None or i>=len(md): continue
            binr=(md[i]>0.5).astype(np.uint8)
            if binr.shape!=(oh,ow): binr=cv2.resize(binr,(ow,oh),interpolation=cv2.INTER_NEAREST)
            polys=mask_polys(binr)
            if polys: out.append(dict(polys=polys))
        return out
    finally:
        os.path.exists(t) and os.unlink(t)

def benchmark(task):
    cfg=TASKS[task]; path=f"{DATA}/{cfg['file']}"
    groups=cfg["prompts"] or [None]
    t0=time.time(); load_s=0; loads=0; runs=0; feats=0
    peak_gpu=[0.0]
    for pg in groups:                       # naive: re-read the whole scene per prompt
        with rasterio.open(path) as ds:
            tl=time.time()
            win=Window(0,0,ds.width,ds.height)
            n=min(3,ds.count)
            arr=ds.read(list(range(1,n+1)), window=win)     # <-- full scene into RAM
            if arr.dtype!=np.uint8: arr=np.clip(arr,0,255).astype(np.uint8)
            region=np.transpose(arr,(1,2,0)); del arr
            load_s+=time.time()-tl; loads+=1
            xs=list(range(0,max(1,region.shape[1]-PATCH+1),PATCH)) or [0]
            ys=list(range(0,max(1,region.shape[0]-PATCH+1),PATCH)) or [0]
            offs=[(x,y) for y in ys for x in xs][:MAX_PATCHES]
            for ox,oy in offs:
                tile=region[oy:oy+PATCH, ox:ox+PATCH]
                if tile.shape[0]<8 or tile.shape[1]<8: continue
                inst = run_sam3(tile,pg) if cfg['kind']=='sam3' else run_yolo(tile, pose=(cfg['kind']=='crown'))
                runs+=1; feats+=len(inst)
                g=subprocess.run(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],
                                 capture_output=True,text=True).stdout.strip().splitlines()
                if g: peak_gpu[0]=max(peak_gpu[0],float(g[0]))
            del region
    e2e=time.time()-t0
    ram=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024  # MiB
    row=dict(environment="Colab", task=task, end_to_end_s=round(e2e,1),
             dataset_load_s=round(load_s,1), model_runs=runs, repeated_data_loads=loads,
             features=feats, peak_ram_gb=round(ram/1024,1), peak_gpu_mib=round(peak_gpu[0]))
    print(json.dumps(row, indent=2))
    open("/content/colab_results.jsonl","a").write(json.dumps(row)+"\n")
    return row
```

---

## 4. Colab — run

```python
# Cell 4a — cheap tasks (should complete on T4/L4)
benchmark("task1")
benchmark("task2")
```

```python
# Cell 4b — the large one. EXPECT AN OOM / KERNEL CRASH on free Colab.
# Reading the full 4.67 GB scene needs ~18.5 GB (int16) + ~9.3 GB (uint8); Colab has ~12.7 GB.
# When the kernel dies, record the Colab row as: "OOM (kernel crash) — RAM exceeded".
benchmark("task3")
```

> **Recording the result:** if task3 crashes the runtime, that is the reportable
> Colab outcome — `naive task3 = OOM at ~12.7 GB`. On Colab Pro (~25–51 GB) it may
> survive partially; note where it dies. Either way it contrasts with GeoTALOS's
> 0.63 GB peak. Copy `/content/colab_results.jsonl` back to compare with
> `evaluation/results/naive.jsonl`.

---

## 5. Folding into the table

Add a `Colab` row per task to `tab:compute-comparison`:
- task1/task2: GPU = whatever Colab assigned (T4/L4), with the printed RAM / load / runtime.
- task3: `Colab & ... & SAM3 & OOM` — the strongest single cell in the comparison.
