# OdorTwin 데모 실행 환경 (conda prefix env: demo\.conda)
# Python 3.11 + PyTorch 2.6 (CUDA 12.4, 서버 da-fno 와 같은 버전) + Node.js 22 (화면 빌드)
$ErrorActionPreference = "Stop"
$envp = Join-Path (Split-Path -Parent $PSScriptRoot) ".conda"
if (-not (Test-Path "$envp\python.exe")) {
    conda create -y -p $envp --override-channels -c conda-forge python=3.11 nodejs=22 pip
}
$py = "$envp\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
& $py -m pip install numpy scipy pandas pyarrow xarray netCDF4 openpyxl matplotlib pillow scikit-image `
    rasterio geopandas pyogrio shapely pyproj overturemaps `
    fastapi "uvicorn[standard]" confluent-kafka paho-mqtt "psycopg[binary]" trino httpx orjson python-dotenv
& $py -c "import torch, rasterio, geopandas, confluent_kafka, paho.mqtt, fastapi; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
& "$envp\node.exe" -v
"ENV_DONE"
