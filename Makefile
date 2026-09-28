# 多品牌电商数据中台 · 常用命令
# Windows 用户若没有 make，直接执行对应的 python -m bdp.cli 命令即可（见 README）

PY ?= python
export PYTHONPATH := src

.PHONY: help install init mock pipeline metrics kb all api test eval store stats clean up down

help:
	@echo "安装：   make install"
	@echo "一键：   make all           （init + mock + pipeline + metrics + kb）"
	@echo "分步：   make init mock pipeline metrics kb"
	@echo "服务：   make api           → http://127.0.0.1:8000/"
	@echo "测试：   make test"
	@echo "评测：   make eval / make eval-chunking"
	@echo "容器：   make up / make down"

install:
	$(PY) -m pip install -r requirements.txt

init:
	$(PY) -m bdp.cli init --drop

mock:
	$(PY) -m bdp.cli mock

pipeline:
	$(PY) -m bdp.cli pipeline

metrics:
	$(PY) -m bdp.cli metrics

kb:
	$(PY) -m bdp.cli kb --rebuild

all:
	$(PY) -m bdp.cli all --drop --rebuild

api:
	$(PY) -m bdp.cli api --reload

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m bdp.cli eval --top-k 5

eval-chunking:
	$(PY) -m bdp.cli eval --chunking

store:
	$(PY) -m bdp.cli store

stats:
	$(PY) -m bdp.cli stats

up:
	docker compose up -d

down:
	docker compose down

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm data/vector_store data/vector_store_eval
