import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import time
import traceback
import sys
import argparse
import base64


def get_screenshot(flag):
    icp_image_base64 = driver.get_screenshot_as_base64()
    imgdata = base64.b64decode(icp_image_base64)
    #将图片保存为文件
    with open(str(flag)+".jpg",'wb') as f:
        f.write(imgdata)

def get_chrome_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument("headless")
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-dev-shm-usage')
    return webdriver.Chrome(options=options)

def Change_The_Time_Type(str_time):
    m, s = str_time.split(':')
    return int(m) * 60 + int(s)
    
def play_one(video_url):
    driver = get_chrome_driver()
    try:
        # 打开视频播放页
        driver.get(video_url)
        
        time.sleep(5)
        
        # 获取视频时长
        #element=driver.find_element_by_xpath("//ul[@class='bilibili-player-video-btn-speed-menu']/li[1]")
        #webdriver.ActionChains(driver).move_to_element(element)
        #Video_Time = driver.find_element(by=By.CSS_SELECTOR, value="span.bilibili-player-video-time-total").text
        #Video_Time = driver.find_element(by=By.CSS_SELECTOR, value="bpx-player-ctrl-time-duration").text
        #获取播放总时长：
        #document.querySelector('video’).duration
        #获取播放进度时长：document.querySelector('video’).currentTime
        #document.querySelector('video').paused
        #这个方法返回的是bool值，true表示暂停状态，false表示播放状态
        currentTime = driver.execute_script('return document.querySelector("video").currentTime')
        Video_Time = driver.execute_script('return document.querySelector("video").duration')
        print("-"*10)
        print(Video_Time)
        print("-"*10)
        flag = 0
        while True:
            flag +=2
            get_screenshot(flag)
            if flag>Video_Time:
                # 看完视频
                if currentTime > Video_Time:
                    print("看完视频----时长达到")
                    return
                time.sleep(Video_Time + 5) # 留出5秒余度
                print("看完视频----留出5秒余度")
                return
            currentTime = driver.execute_script('return document.querySelector("video").currentTime')
            print("当前播放时间{}".format(currentTime))
            time.sleep(2)
    
    except Exception as e:
        traceback.print_exc()
    finally:
        # 关闭页面
        driver.close()

    return None

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--video_url", dest='video_url', help="视频链接")
    
    args = parser.parse_args()
    video_url = args.video_url
    video_url = str(base64.b64decode(video_url),"utf-8")
    play_one(video_url)
