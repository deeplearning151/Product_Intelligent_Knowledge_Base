# 导入Python内置模块
import os
import json
# 导入MinIO官方Python SDK核心类
from minio import Minio
# 项目内部配置与日志
from app.conf.minio_config import minio_config
from app.core.logger import logger

# 全局MinIO客户端对象，初始化后供全项目调用
minio_client = None


# 初始化MinIO客户端实例，相当于你登陆了minio
minio_client = Minio(
    # 端点
    endpoint=minio_config.endpoint,
    # 账号
    access_key=minio_config.access_key,
    # 密码
    secret_key=minio_config.secret_key,
    secure=False  # 内网/本地部署用HTTP，公网部署需改为True并配置SSL
)

# 创建一个桶，没有才创建
bucket_name = minio_config.bucket_name

if not minio_client.bucket_exists(bucket_name):
    # 不存在桶，创建并设置访问权限
    minio_client.make_bucket(bucket_name)
    # 设置桶的访问权限
    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket_name}/*"]
            }]
    }
    minio_client.set_bucket_policy(bucket_name, json.dumps(bucket_policy))

def get_minio_client():
    return minio_client