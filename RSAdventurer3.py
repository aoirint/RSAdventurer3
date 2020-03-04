
import socket
import os
import subprocess
import re
import json
import sqlite3
import requests
import io
from PIL import Image

from datetime import datetime as dt
from pytz import timezone
import json

import flashforge_finder_api.api.protocol as ffapi


ADVENTURER3_MACADDRESS = os.environ.get('ADVENTURER3_MACADDRESS', '00:00:00:00:00:00')

TIMEZONE = os.environ.get('TIMEZONE', 'Asia/Tokyo')
WEB_ROOT_URL = os.environ.get('WEB_ROOT_URL', 'http://example.com')

POST_TEAMS = os.environ.get('POST_TEAMS') == '1'
TEAMS_INCOMING_WEBHOOK_URL = os.environ.get('TEAMS_INCOMING_WEBHOOK_URL')
if TEAMS_INCOMING_WEBHOOK_URL is None:
    print('Teams incoming webhook URL not provided')
    POST_TEAMS = False


ip = None
mac = ADVENTURER3_MACADDRESS


def find_with_mac():
    ip_ = None

    p = subprocess.Popen([ 'arp', '-n' ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()

    out = out.decode('utf-8')
    lines = out.split('\n')
    for line in lines[1:]:
        cols = re.split('\s+', line)
        mac_ = cols[2]

        if mac == mac_:
            ip_ = cols[0]
            break

    return ip_


def check_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2) # 2 sec
    result = sock.connect_ex((ip, port)) # 8899 is print port

    return result == 0

# TODO: improvement: post to teams every 5 minutes; more clear way
local_post_teams = True
local_post_teams_counter = 0
def run():
    global local_post_teams, local_post_teams_counter

    ip = find_with_mac()
    print(ip)

    portPrint = check_port(8899)
    portHttp = check_port(8080)

    print(portPrint)
    print(portHttp)

    img = None
    isDown = True
    imgdir = 'www/printer3d/snapshot'

    timestamp = dt.now(timezone(TIMEZONE))
    if portHttp:
        state = 'Printing'

        from urllib.request import urlopen
        stream = urlopen('http://' + ip + ':8080/?action=stream')
        binary = b''
        while True:
            binary += stream.read(1024)
            start_marker = binary.find(b'\xff\xd8')
            end_marker = binary.find(b'\xff\xd9')

            if start_marker != 1 and end_marker != -1:
                binary = binary[start_marker:end_marker+2]
                break

        #r = requests.get('http://' + ip + ':8080?action=snapshot')
        #ct = r.content # jpg#
        bt = io.BytesIO(binary)

        im = Image.open(bt)
        im = im.crop((60, 0, im.width - 60, im.height)) # 画角広すぎて外写るのでクロップ

        imgname = 'snapshot_%02d%02d.jpg' % (timestamp.hour, timestamp.minute)
        os.makedirs(imgdir, exist_ok=True)

        img_to_save = os.path.join(imgdir, imgname)#
        with open(img_to_save, 'wb') as fp:
            fp.write(binary)
        im.save(os.path.join(imgdir, imgname))

        img_url_path = '/printer3d/snapshot/%s?%s' % (imgname, timestamp.timestamp())
        img = WEB_ROOT_URL + img_url_path

        isDown = False
    #elif portPrint:
    if portPrint:
        isDown = False
        pass
        #state = 'Idle'
    else:
        print('3dprinter is down')

    ssdata = {}
    snapshot_path = os.path.join(imgdir, 'snapshot.json')
    if os.path.exists(snapshot_path):
        with open(snapshot_path, 'r') as fp:
            ssdata = json.load(fp)


    isPrinting = False

    temp = None
    progress = {}
    status = None
    ffapiTimeout = None

    address = { 'ip': ip, 'port': 8899, }

    if not isDown:
        if portPrint:
            try:
                temp = ffapi.get_temp(address)
                progress = ffapi.get_progress(address)
                status = ffapi.get_status(address)

                ffapiTimeout = False
            except socket.timeout:
                print('socket timeout')
                ffapiTimeout = True

    state = status['Status'] if status is not None else 'UNKNOWN'
    if state is not None:
        isPrinting = state != 'READY'

    percentageCompleted = progress.get('PercentageCompleted', 0)

    prevDown = ssdata.get('is_down', True)
    prevPrinting = ssdata.get('is_printing', False)
    prevProgress = ssdata.get('progress', {})
    prevPercentageCompleted = prevProgress.get('PercentageCompleted', 0)
    prevProgressed = ssdata.get('progressed', False)

    newPrintStart = isPrinting and not prevPrinting
    newPowerOn = not isDown and prevDown
    newPowerOff = isDown and not prevDown
    progressed = percentageCompleted > prevPercentageCompleted

    rawProgressedProgress = ssdata.get('progressed_progress', progress)
    progressedPercentageCompleted = rawProgressedProgress.get('PercentageCompleted', 0)
    progressedProgress = rawProgressedProgress if not progressed else progress

    rawPrevProgressedProgress = ssdata.get('prev_progressed_progress', rawProgressedProgress)
    prevProgressedPercentageCompleted = rawPrevProgressedProgress.get('PercentageCompleted', 0)
    prevProgressedProgress = rawPrevProgressedProgress if not progressed else rawProgressedProgress

    powerOnTimestamp = dt.fromisoformat(ssdata.get('poweron_timestamp', timestamp.isoformat())) if not newPowerOn else timestamp
    printStartTimestamp = dt.fromisoformat(ssdata.get('printstart_timestamp', timestamp.isoformat())) if not newPrintStart else timestamp

    rawProgressTimestamp = dt.fromisoformat(ssdata.get('progressed_timestamp', timestamp.isoformat()))
    progressedTimestamp = rawProgressTimestamp if not progressed else timestamp
    rawPrevProgressedTimestamp = dt.fromisoformat(ssdata.get('prev_progressed_timestamp', rawProgressTimestamp.isoformat()))
    prevProgressedTimestamp = rawPrevProgressedTimestamp if not progressed else rawProgressTimestamp
    #powerOnTimestamp = printStartTimestamp

    estimatedPrintTimeLeft = None
    if percentageCompleted > 1: # Required over 2%; because 0% includes long heating
        if progressedPercentageCompleted != prevProgressedPercentageCompleted:
            progressedPercentageLeft = 100 - progressedPercentageCompleted
            onePercentPrintTime = (progressedTimestamp - prevProgressedTimestamp) / (progressedPercentageCompleted - prevProgressedPercentageCompleted)
            estimatedPrintTimeLeft = onePercentPrintTime * progressedPercentageLeft

    powerOnElapsed = timestamp - powerOnTimestamp
    printingElapsed = timestamp - printStartTimestamp


    postFlag = not isDown or newPowerOff

    flag_post_teams = POST_TEAMS and local_post_teams
    if postFlag and flag_post_teams:
        state_post = state if not isDown else 'DOWN'
        msg = 'Status: %s (Timestamp: %s)\n\n' % (state, timestamp, )
        msg += 'LAN IP: %s\n\n' % ip

        if ffapiTimeout:
            msg += 'API Timeout, 接続失敗, 他に接続中のコンピュータがあります\n\n'

        if progress is not None:
            msg += 'Progress: %s %% (%s/%s Bytes)\n\n' % ( progress['PercentageCompleted'], progress['BytesPrinted'], progress['BytesTotal'] )

        if estimatedPrintTimeLeft is not None:
            estimatedPrintEndTimestamp = timestamp + estimatedPrintTimeLeft
            msg += '(EXPERIMENTAL) Estimated Print Time Left: %s / ' % estimatedPrintTimeLeft
            msg += 'Finish: %s\n\n' % estimatedPrintEndTimestamp
        else:
            msg += '(EXPERIMENTAL) Estimated Print Time: Estimating\n\n'

        if temp is not None:
            msg += 'Temperature: %s/%s ℃\n\n' % ( temp['Temperature'], temp['TargetTemperature'] )
            msg += 'Platform Temperature: %s/%s ℃\n\n' % ( temp['BaseTemperature'], temp['TargetBaseTemperature'] )

        if img is not None:
            msg += '[Image](%s)\n\n ' % img

        if isPrinting:
            msg += 'Printing Elapsed: %s / ' % printingElapsed
        msg += 'Power-On Elapsed: %s\n\n' % powerOnElapsed

        web = WEB_ROOT_URL + '/printer3d/'
        msg += '[Web](%s)' % web

        print(msg)

        data = {
            'title': '3DPrinter Notification',
            'text': msg,
        }
        #if img is not None:
        #    data['heroImage'] = img

        print(data)

        requests.post(TEAMS_INCOMING_WEBHOOK_URL, data=json.dumps(data), headers={
            'Content-Type': 'application/json',
        })

    ssdata = {
        'address': address,
        'is_down': isDown,
        'is_printing': isPrinting,
        'prev_down': prevDown,
        'prev_printing': prevPrinting,
        'apiTimeout': ffapiTimeout,
        'temperature': temp,
        'progress': progress,
        'progressed': progressed,
        'progressed_progress': progressedProgress,
        'prev_progressed_progress': prevProgressedProgress,
        'status': status,
        'last_img': img,
        'timestamp': timestamp.isoformat(),
        'poweron_timestamp': powerOnTimestamp.isoformat(),
        'printstart_timestamp': printStartTimestamp.isoformat(),
        'progressed_timestamp': progressedTimestamp.isoformat(),
        'prev_progressed_timestamp': prevProgressedTimestamp.isoformat(),
    }
    with open(os.path.join(imgdir, 'snapshot.json'), 'w') as fp:
        json.dump(ssdata, fp)
    with open(os.path.join(imgdir, 'snapshot_%02d%02d.json' % (timestamp.hour, timestamp.minute)), 'w') as fp:
        json.dump(ssdata, fp)

    local_post_teams_counter += 1
    local_post_teams = local_post_teams_counter % 5 == 0
    if local_post_teams:
        local_post_teams_counter = 0


if __name__ == '__main__':
    import schedule

    schedule.every(1).minutes.do(run)

    while True:
        schedule.run_pending()
        time.sleep(1)
