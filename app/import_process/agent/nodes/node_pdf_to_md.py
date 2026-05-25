import os
import shutil
import sys
import zipfile

import requests
import time

from pathlib import Path
from app.core.logger import logger
from app.import_process.agent.state import ImportGraphState, create_default_state
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import add_running_task, add_done_task
from app.conf.mineru_config import mineru_config

"""
    node_pdf_to_md
        参数：state[is_pdf_read_enabled = True | pdf_path = xxx.pdf | local_dir = output]
        返回：state[md_path = 地址 | md_content = 内容]
        1. 日志和任务状态
        2. step1_validate_path路径校验
        3. step2_upload_and_poll minerU的交互
        4. step3_download_and_extract下载和解压
        5. 日志和任务状态return state
    step1_validate_paths
        参数：state pdf_path = xxx.pdf | local_dir = output
        返回：pdf_path_obj Path local_dir_obj Path
        1. 非空校验
        2. 文件校验pdf_path_obj没有抛异常 local_dir_obj没有给予默认
        3.返回完成可用的Path对象即可
    
    step2_upload_and_poll
        参数：pdf对应Path pdf_path_obj
        返回：str zip url地址
        1. 进行申请，获取要上传文件的地址
        2. 进行文件上传 session | requests.put
        3. 轮询获取返回结果 zip_url 确定一个最大等待时间 1页pdf 1s 间隔时间 3s 错误码200 500能容忍
        4. 返回地址即可
    step3_download_and_extract、
        参数:zip_url, out_dir_obj，原文件名path.stem
        返回：解压厚得.md的str地址
        1. zip下载get output / stem_result.zip
        2. 检查解压的文件夹地址 output / stem
        3. 检查解压的文件夹防重复处理
        4. 进行解压zipFile extractall 解压的目标文件夹
        5. 考虑文件名字 原文件名 还是full 还是其他
        6. 重命名处理
        7. 路径转成字符串 获取绝对路径最终返回即可
"""


def step1_validate_paths(state):
    """
    进行路径校验 pdf_path失效，直接异常处理；local_dir 没有给予默认值
    :param state:
    :return:
    """
    logger.debug(f">>> [step1_validate_paths]在md转pdf下，开始进行文件格式校验！")
    pdf_path = state['pdf_path']
    local_dir = state['local_dir']
    # 常规的非空校验（站在字符串的角度）
    if not pdf_path:
        logger.error(f"[step1_validate_paths]检查没有发现输入文件，无法继续解析！")
        raise ValueError("[step1_validate_paths]检查没有发现输入文件，无法继续解析！")
    if not local_dir:
        # 给予一个默认值
        local_dir = str(PROJECT_ROOT / "output")
        logger.info(f"[step1_validate_paths]检查发现local_dir没有赋值，给予默认值：{local_dir}")
    #进行文件存在校验
    pdf_path_obj = Path(pdf_path)
    local_dir_obj = Path(local_dir)
    if not pdf_path_obj.exists():
        logger.error(f"[step1_validate_paths]检查发现pdf_path不存在，请检查输入文件路径是否正确！")
        raise FileNotFoundError(f"[step1_validate_paths]检查发现pdf_path不存在，请检查输入文件路径是否正确！")
    if not local_dir_obj.exists():
        logger.error(f"[step1_validate_paths]检查发现local_dir不存在，请主动创建对应的文件夹！")
        local_dir_obj.mkdir(parents=True, exist_ok=True)
    
    return pdf_path_obj, local_dir_obj

def step2_upload_and_poll(pdf_path_obj):
    """
    将pdf文件使用minerU解析，并且获取md对应的下载url地址
    :param pdf_path_obj:上传解析pdf文件的path对象
    :return:str -> url ，minerU解析后md文件zip压缩包的下载地址
    """
    # 1. 申请上传解析的地址
    token = mineru_config.api_key
    url = f"{mineru_config.base_url}/file-urls/batch"
    header = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    data = {
        "files": [
            {"name": f"{pdf_path_obj.name}"}
        ],
        "model_version": "vlm"
    }
    response = requests.post(url, headers=header, json=data)
    #结果处理 请求http状态码不是200 或者 返回结果的状态码不是0 请求失败
    if response.status_code != 200 or response.json()['code'] != 0:
        raise RuntimeError(f"[step2_upload_and_poll]请求mineru解析接口失败，请检查输入文件路径是否正确！")
    uploaded_url = response.json()['data']['file_urls'][0] # 上这个地址传文件
    batch_id = response.json()['data']['batch_id'] # 处理id，后续根据这个id获取结果

    # 2. 将文件上传到对应的解析地址
    # 使用put请求，将pdf_path_obj文件传递到uploaded_url地址即可
    # 注意 不能直接使用put，很大概率报错，电脑开了各种代理，put的请求头，添加一些额外的参数头，将文件真的转存到第三方文件存储服务器
    # 文件储存服务器检查比较严格，拒绝存储！报错！get post 宽进宽出 put严进严出
    http_sesson = requests.Session()
    http_sesson.trust_env = False
    try:
        with open(pdf_path_obj, 'rb') as f:
            file_data = f.read()
        upload_response = http_sesson.put(uploaded_url, data=file_data)
        if upload_response.status_code != 200:
            logger.error(f"[step2_upload_and_poll]上传文件到minerU失败，请检查输入文件路径是否正确！")
            raise RuntimeError(f"[step2_upload_and_poll]上传文件到minerU失败，请检查输入文件路径是否正确！")
    except Exception as e:
        logger.error(f"[step2_upload_and_poll]上传文件到minerU失败，请检查输入文件路径是否正确！")
        raise RuntimeError(f"[step2_upload_and_poll]上传文件到minerU失败，请检查输入文件路径是否正确！")
    finally:
        http_sesson.close()

    # 3. 轮询获取解析结果
    # 循环获取，确保获取到结果，再先后执行！
    # 设计一个循环，3秒获取一次，最多等待10分钟600秒 -> 600页pdf
    url = f"{mineru_config.base_url}/extract-results/batch/{batch_id}"
    timeout_seconds = 600
    poll_interval = 3
    start_time = time.time() # 进入的起始时间
    while True:
        # 3.1 超时判断，不能站在第一次的角度，站在宏观的角度
        if time.time() - start_time > timeout_seconds:
            logger.error(f"[step2_upload_and_poll]请求minerU解析接口超时，请检查输入文件路径是否正确！")
            raise RuntimeError(f"[step2_upload_and_poll]请求minerU解析接口超时，请检查输入文件路径是否正确！")
        # 3.2 向指定的url地址获取本次解析的结果
        res = requests.get(url, headers=header)
        # 3.3 解析结果判断和获取zip_url
        if res.status_code != 200:
            # 抛出异常
            if 500 <= res.status_code < 600:
               time.sleep(poll_interval)
               continue
            raise RuntimeError(f"[step2_upload_and_poll]请求minerU解析接口失败，返回的状态码为：{res.status_code}！")

        json_data = res.json()
        if json_data['code'] != 0:
            # ！=很大概率token过期了
            raise RuntimeError(f"[step2_upload_and_poll]请求minerU解析接口失败，返回的错误：{json_data['code']}信息为：{json_data['mes']}！")

        # 判断下解析状态
        extract_result = json_data['data']['extract_result'][0]
        if extract_result['state'] == 'done':
            #解析完毕可以获取结果
            full_zip_url = extract_result['full_zip_url']
            logger.info(f"[step2_upload_and_poll]minerU解析成功，耗时：{time.time() - start_time}秒，解析结果为：{full_zip_url}")
            return full_zip_url
        else:
            #还没解析完
            time.sleep(poll_interval)

def step3_download_and_extract(zip_url, local_dir_obj, stem):
    """
    下载指定的md.zip文件，并且解压，返回解压后的md文件的地址
    :param zip_url: 要下载的地址
    :param local_dir_obj: 存储的文件夹
    :param stems: pdf的文件名字
    :return: 返回md文件的地址
    """
    # 1. 下载zip文件 response响应体
    response = requests.get(zip_url)

    if response.status_code != 200:
        logger.error(f"[step3_download_and_extract]下载zip文件失败，请检查输入文件路径是否正确！")
        raise RuntimeError(f"[step3_download_and_extract]下载zip文件失败，请检查输入文件路径是否正确！")
    # 2. 将响应体的zip文件保存到本地
    # 保存文件output/xx手册/xx手册_result.zip
    zip_save_path = local_dir_obj / f"{stem}_result.zip"
    with open(zip_save_path, 'wb') as f:
        # response.content响应体中的数据
        f.write(response.content)
    logger.info(f"[step3_download_and_extract]下载zip文件成功，保存路径为：{zip_save_path}")

    # 3. 清空下载旧目录（将上一次处理的文件目录进行删除）
    extract_target_dir = local_dir_obj / stem
    # 先清空文件内容，因为两次解压的文件数量可能不一样，会保留旧数据
    if extract_target_dir.exists():
        shutil.rmtree(extract_target_dir)
    # 创建新的目录
    extract_target_dir.mkdir(parents=True, exist_ok=True)
    # 4. 进行zip文件的解压工作
    # python zip解压模块 zipfile进行zip压缩和解压
    with zipfile.ZipFile(zip_save_path, 'r') as zip_file_object:
        zip_file_object.extractall(extract_target_dir)
    # 5. 返回md文件的地址
    # 解压后的文件名可能叫文件.md 也可能叫full.md
    md_file_list = list(extract_target_dir.rglob("*.md"))

    if not md_file_list:
        logger.error(f"[step3_download_and_extract]没有找到md文件，请检查输入文件路径是否正确！")
        raise RuntimeError(f"[step3_download_and_extract]没有找到md文件，请检查输入文件路径是否正确！")

    target_md_file = None # 存储最终md文件
    # 检查有没有源文件名的md
    for md_file in md_file_list:
        # stem 文件名
        if md_file.name == stem + ".md":
            target_md_file = md_file
            break
    # 检查有没有full.md
    if not target_md_file:
        for md_file in md_file_list:
            # stem 文件名
            if md_file.name.islower == "full.md":
                target_md_file = md_file
                break

    # 是在没有就获取第一个
    if not target_md_file:
        target_md_file = md_file_list[0]

    # md文件名 xx手册.md full.md 不知道.md
    # 统一改成 原文件名(stem).md
    # 不是原名字的时候，才重命名
    if target_md_file.stem != stem:
        # 进行重命名
        # target_md_file.with_name(f"{stem}.md")修改path对象，不涉及文件操作，返回结果是修改后path对象
        # target_md_file.rename(target_md_file.with_name(f"{stem}.md"))修改磁盘中的文件名称
        target_md_file = target_md_file.rename(target_md_file.with_name(f"{stem}.md"))

    # 最终的md文件获取绝对路径，并且返回字符串类型
    final_md_str_path = str(target_md_file.resolve())
    logger.info(f"[step3_download_and_extract]解压zip文件成功，保存路径为：{final_md_str_path}")
    return final_md_str_path

def node_pdf_to_md(state: ImportGraphState) -> ImportGraphState:
    """
    节点: PDF转Markdown (node_pdf_to_md)
    为什么叫这个名字: 核心任务是将 PDF 非结构化数据转换为 Markdown 结构化数据。
    未来要实现:
        1. 进入的日志和任务状态的配置
        2. 进行参数校验（local_dir -> 给予默认值 | local_file_path完成字面意思的校验 -> 深入校验校验文件是否真实存在）
        3. 调用minerU进行pdf解析（local_file_path）返回一个下载文件的地址 xx.zip url地址
        4. 下载zip包，并且解压和提取（local_dir）
        5. 把md_path地址进行赋值，读取md的文件内容， md_content赋值（文本内容）
        6. 结束的日志和任务状态的配置
        容错率处理，try异常处理
    """
    function_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{function_name}]开始执行了！现在的状态为：{state}")
    add_running_task(state['task_id'], function_name)

    try:
        # 2.进行参数校验（local_dir -> 给予默认值 | local_file_path完成字面意思的校验 -> 深入校验校验文件是否真实存在）
        # 参数：state local_file_path | local_dir
        # 返回：校验后的文件和输出文件夹Path对象
        pdf_path_obj, local_dir_obj = step1_validate_paths(state)
        # 3.调用minerU进行pdf解析（local_file_path）返回一个下载文件的地址 xx.zip url地址
        # 参数：要解析的pdf文件路径 返回值：要下载的zip文件地址
        zip_url = step2_upload_and_poll(pdf_path_obj)
        # 4.下载zip包，并且解压和提取（local_dir）
        # 参数：1.要下载的地址 2.local_dir_obj解压的文件夹 3.文件名 xx手册（xx手册.pdf）
        md_path = step3_download_and_extract(zip_url, local_dir_obj, pdf_path_obj.stem)
        # 5. 把md_path地址进行赋值，读取md的文件内容 md_content赋值（文本内容）
        # 更新数据
        state['md_path'] = md_path
        state['local_dir']= str(local_dir_obj)
        # md的内容读取，配置给md_content
        with open(md_path, 'r', encoding='utf-8') as f:
            state['md_content'] = f.read()
    except Exception as e:
        # 处理异常
        logger.error(f">>>[{function_name}]使用minerU解析发生了异常，异常信息：{e}")
        raise # 终止工作流
    finally:
        #6. 结束的日志和任务状态的配置
        logger.info(f">>> [{function_name}]结束了！现在的状态为：{state}")
        add_done_task(state['task_id'], function_name)

    logger.info(f">>> [Stub] 执行节点: {sys._getframe().f_code.co_name}")
    return state


if __name__ == "__main__":

    # 单元测试：验证PDF转MD全流程
    logger.info("===== 开始node_pdf_to_md节点单元测试 =====")

    from app.utils.path_util import PROJECT_ROOT
    logger.info(f"测试获取根地址：{PROJECT_ROOT}")

    test_pdf_name = os.path.join("doc", "hak180产品安全手册.pdf")
    test_pdf_path = os.path.join(PROJECT_ROOT, test_pdf_name)

    # 构造测试状态
    test_state = create_default_state(
        task_id="test_pdf2md_task_001",
        pdf_path=test_pdf_path,
        local_dir=os.path.join(PROJECT_ROOT, "output")
    )

    node_pdf_to_md(test_state)

    logger.info("===== 结束node_pdf_to_md节点单元测试 =====")