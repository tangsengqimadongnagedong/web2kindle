# !/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Vincent<vincent8280@outlook.com>
#         http://wax8280.github.io
# Created on 2017/10/10 14:05
import os
import re
import time
from copy import deepcopy
from queue import Queue, PriorityQueue
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

from web2kindle import MAIN_CONFIG
from web2kindle.libs.crawler import Crawler, RetryDownload, Task
from web2kindle.libs.db import ArticleDB
from web2kindle.libs.html2kindle import HTML2Kindle
from web2kindle.libs.send_email import SendEmail2Kindle
from web2kindle.libs.utils import write, md5string, load_config, check_config
from web2kindle.libs.log import Log

SCRIPT_CONFIG = load_config('./web2kindle/config/zhihu_zhuanlan.yml')
LOG = Log("zhihu_zhuanlan")
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/'
                  '61.0.3163.100 Safari/537.36'
}

check_config(MAIN_CONFIG, SCRIPT_CONFIG, 'SAVE_PATH', LOG)
ARTICLE_ID_SET = set()


def main(zhuanlan_name_list, start, end, kw):
    iq = PriorityQueue()
    oq = PriorityQueue()
    result_q = Queue()
    crawler = Crawler(iq, oq, result_q, MAIN_CONFIG.get('PARSER_WORKER', 1), MAIN_CONFIG.get('DOWNLOADER_WORKER', 1),
                      MAIN_CONFIG.get('RESULTER_WORKER', 1))
    new = True

    for zhuanlan_name in zhuanlan_name_list:
        new_header = deepcopy(DEFAULT_HEADERS)
        new_header.update({'Referer': 'https://zhuanlan.zhihu.com/{}'.format(zhuanlan_name)})
        save_path = os.path.join(SCRIPT_CONFIG['SAVE_PATH'], str(zhuanlan_name))

        task = Task.make_task({
            'url': 'https://zhuanlan.zhihu.com/api/columns/{}/posts?limit=20&offset={}'.format(zhuanlan_name, start),
            'method': 'GET',
            'meta': {'headers': new_header, 'verify': False},
            'parser': parser_list,
            'priority': 0,
            'save': {'cursor': start,
                     'save_path': save_path,
                     'start': start,
                     'end': end,
                     'kw': kw,
                     'name': zhuanlan_name},
            'retry': 3,
        })

        iq.put(task)
        # Init DB
        with ArticleDB(save_path, VERSION=0) as db:
            _ = db.select_all_article_id()
        if _:
            for each in _:
                ARTICLE_ID_SET.add(each[0])

    crawler.start()
    for zhuanlan_name in zhuanlan_name_list:
        items = []
        book_name = '知乎专栏_{}'.format(zhuanlan_name)
        save_path = os.path.join(SCRIPT_CONFIG['SAVE_PATH'], str(zhuanlan_name))
        with ArticleDB(save_path, VERSION=0) as db:
            db.insert_meta_data(['BOOK_NAME', zhuanlan_name])
            items.extend(db.select_article())
            db.increase_version()
            db.reset()

        if items:
            new = True
            with HTML2Kindle(items, save_path, book_name, MAIN_CONFIG.get('KINDLEGEN_PATH')) as html2kindle:
                html2kindle.make_metadata(window=kw.get('window', 50))
                html2kindle.make_book_multi(save_path)
        else:
            LOG.log_it('无新项目', 'INFO')
            new = False

    if new and kw.get('email'):
        for zhuanlan_name in zhuanlan_name_list:
            with SendEmail2Kindle() as s:
                s.send_all_mobi(os.path.join(SCRIPT_CONFIG['SAVE_PATH'], str(zhuanlan_name)))


def parser_list(task):
    response = task['response']
    new_tasks = []
    to_next = True

    if not response:
        raise RetryDownload

    try:
        data = response.json()
        data.reverse()
    except Exception as e:
        LOG.log_it('解析JSON出错（如一直出现，而且浏览器能正常访问知乎，可能是知乎代码升级，请通知开发者。）ERRINFO:{}'
                   .format(str(e)), 'WARN')
        raise RetryDownload

    for item in data:
        # 如果在数据库里面已经存在的项目，就不继续爬了
        url = 'https://zhuanlan.zhihu.com' + item['url']
        if md5string(url) in ARTICLE_ID_SET:
            to_next = False
            break

        new_task = Task.make_task({
            'url': url,
            'method': 'GET',
            'meta': task['meta'],
            'parser': parser_content,
            'resulter': resulter_content,
            'priority': 5,
            'save': task['save'],
            'title': item['title'],
        })
        new_tasks.append(new_task)

    # 下一页
    if to_next and len(data) != 0:
        if task['save']['cursor'] < task['save']['end'] - 20:
            next_page_task = deepcopy(task)
            next_page_task.update(
                {'url': re.sub('offset=\d+', 'offset={}'.format(task['save']['cursor'] + 20), next_page_task['url'])})
            next_page_task['save'].update({'cursor': next_page_task['save']['cursor'] + 20})
            new_tasks.append(next_page_task)

    return None, new_tasks


def parser_content(task):
    title = task['title']
    new_tasks = []

    response = task['response']
    if not response:
        raise RetryDownload

    bs = BeautifulSoup(response.text, 'lxml')

    content_tab = bs.select('.PostIndex-content')
    if content_tab:
        content = str(content_tab[0])
    else:
        LOG.log_it("不能找到文章的内容。（如一直出现，而且浏览器能正常访问知乎，可能是知乎代码升级，请通知开发者。）", 'WARN')
        raise RetryDownload

    author_name = bs.select('.PostIndex-authorName')[0].string if bs.select('.PostIndex-authorName') else ''
    voteup_count = re.search('likesCount&quot;:(\d+),', response.text).group(1) if re.search(
        'likesCount&quot;:(\d+),', response.text) else ''
    created_time = str(bs.select('.PostIndex-header .HoverTitle')[1]['data-hover-title']) if len(
        bs.select('.PostIndex-header .HoverTitle')) == 2 else ''
    article_url = task['url']

    download_img_list, content = format_zhihu_content(content, task)

    item = [md5string(article_url), title, content, created_time, voteup_count, author_name,
            int(time.time() * 100000)]

    if task['save']['kw'].get('img', True):
        img_header = deepcopy(DEFAULT_HEADERS)
        img_header.update({'Referer': response.url})
        for img_url in download_img_list:
            new_tasks.append(Task.make_task({
                'url': img_url,
                'method': 'GET',
                'meta': {'headers': img_header, 'verify': False},
                'parser': parser_downloader_img,
                'resulter': resulter_downloader_img,
                'save': task['save'],
                'priority': 10,
            }))

    task.update({"parsed_data": item})
    return task, new_tasks


def resulter_content(task):
    LOG.log_it("正在将任务 {} 插入数据库".format(task['tid']), 'INFO')
    with ArticleDB(task['save']['save_path']) as article_db:
        article_db.insert_article(task['parsed_data'])


def parser_downloader_img(task):
    return task, None


"""
在convert_link函数里面md5(url)，然后转换成本地链接
在resulter_downloader_img函数里面，将下载回来的公式，根据md5(url)保存为文件名
"""


def resulter_downloader_img(task):
    if 'www.zhihu.com/equation' not in task['url']:
        write(os.path.join(task['save']['save_path'], 'static'), urlparse(task['response'].url).path[1:],
              task['response'].content, mode='wb')
    else:
        write(os.path.join(task['save']['save_path'], 'static'), md5string(task['url']) + '.svg',
              task['response'].content,
              mode='wb')


def convert_link(x):
    if 'www.zhihu.com/equation' not in x.group(1):
        return 'src="./static/{}"'.format(urlparse(x.group(1)).path[1:])
    # svg等式的保存
    else:
        url = x.group(1)
        if not url.startswith('http'):
            if url.startswith('//'):
                url = 'http:' + url
            else:
                url = 'http://' + url
        a = 'src="./static/{}.svg"'.format(md5string(url))
        return a


def format_zhihu_content(content: str, task):
    """去除空行-删除无用img标签-img居中-清除gif-移除html和body标签-获取静态资源下载地址-将静态资源的地址转换为本地路径-超链接的转换-noscript标签移除"""
    download_img_list = []
    # 换行格式化
    content = content.replace('</p><br/><p>', '<br/>').replace('</p><p><br/>', '').replace('</p><p><br>', '')
    content = re.sub('(<br>)+', '<br/>', content)
    content = re.sub('(<br/>)+', '<br/>', content)

    bs2 = BeautifulSoup(content, 'lxml')
    for tab in bs2.select('img[src^="data"]'):
        # 删除无用的img标签
        tab.decompose()
    # 居中图片
    for tab in bs2.select('img'):
        if 'equation' not in tab['src']:
            tab.wrap(bs2.new_tag('div', style='text-align:center;'))
            tab['style'] = "display: inline-block;"

        # 删除gif
        if task['save']['kw']['gif'] is False:
            if 'gif' in tab['src']:
                tab.decompose()

    content = str(bs2)
    # bs4会自动加html和body 标签
    content = re.sub('<html><body>(.*?)</body></html>', lambda x: x.group(1), content, flags=re.S)

    # 公式地址转换（傻逼知乎又换地址了）
    # content = content.replace('//www.zhihu.com', 'http://www.zhihu.com')

    # 需要下载的静态资源
    download_img_list.extend(re.findall('src="(.*?)"', content))
    # 更换为本地相对路径
    content = re.sub('src="(.*?)"', convert_link, content)

    # 超链接的转换
    content = re.sub('//link.zhihu.com/\?target=(.*?)"', lambda x: unquote(x.group(1)), content)
    content = re.sub('<noscript>(.*?)</noscript>', lambda x: x.group(1), content, flags=re.S)
    return download_img_list, content
