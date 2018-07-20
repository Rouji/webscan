#!/usr/bin/env python3
# coding=utf-8

import random
import string
from io import BytesIO
from typing import List

import cv2
import sane
from PIL import Image
from entrypoint2 import entrypoint
from flask import Flask, send_file, request, jsonify, render_template, url_for, make_response, redirect
from numpy import array
from smtplib import SMTP_SSL as smtp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import COMMASPACE, formatdate
import zipfile

import config


def rnd(length: int):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


app = Flask(__name__)
app.secret_key = rnd(40)

scanned_images = {}


def pil_to_jpeg(pil_img, quality):
    sio = BytesIO()
    pil_img.save(sio, 'JPEG', quality=quality)
    sio.seek(0)
    return sio.read()




def clamp(value, minv, maxv):
    return max(min(value, maxv), minv)


def points_bounding_rect(points: List[tuple], pad: int = 0, clamp_area: tuple = None):
    x = [int(p[0]) for p in points]
    y = [int(p[1]) for p in points]
    if not pad:
        return min(x) - pad, min(y) - pad, max(x) + pad, max(y) + pad
    return (clamp(min(x) - pad, 0, clamp_area[0]),
            clamp(min(y) - pad, 0, clamp_area[1]),
            clamp(max(x) + pad, 0, clamp_area[0]),
            clamp(max(y) + pad, 0, clamp_area[1]))


def auto_crop(pil_img):
    aspect = pil_img.size[1] / pil_img.size[0]
    scaled_width = 1000
    scaled_size = (scaled_width, int(scaled_width * aspect))
    cv_img = array(pil_img)
    orig = cv_img.copy()
    cv_img = cv2.resize(cv_img, scaled_size)
    cv_img = cv2.cvtColor(cv_img, cv2.COLOR_RGB2GRAY)
    cv_img = cv2.GaussianBlur(cv_img, (5, 5), 0)

    edges = cv2.Canny(cv_img, 0, 50)
    (_, contours, _) = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    # get all large rectangles
    bounding_points = []
    for c in contours:
        (x, y, w, h) = cv2.boundingRect(c)
        if w > scaled_width / 10 and h > scaled_width / 10:
            bounding_points.append((x, y))
            bounding_points.append((x + w, y + h))

    # get an area containing all rectangles and crop
    ratio = pil_img.size[0] / scaled_width
    (x1, y1, x2, y2) = tuple(int(p * ratio) for p in points_bounding_rect(bounding_points, int(scaled_width / 100), scaled_size))
    return Image.fromarray(orig[y1:y2, x1:x2])


@app.route('/scan')
def scan():
    sane.init()
    devices = sane.get_devices(True)
    if not devices:
        sane.exit()
        return jsonify({'success': False, 'error': 'No scanner found.'}), 404
    try:
        dev = sane.open(devices[0][0])
        #param = dev.get_parameters()
        dev.start()
        im = dev.snap()
        dev.close()
    except Exception as ex:
        sane.exit()
        return jsonify({'success': False, 'error': 'Scanning failed. Scanner might not be ready or turned off.'}), 500
    if 'crop' in request.values:
        im = auto_crop(im)
    thumb = im.copy()
    thumb.thumbnail((config.THUMB_SIZE, config.THUMB_SIZE), Image.ANTIALIAS)
    id = rnd(20)
    scanned_images[id] = (
        pil_to_jpeg(im, config.SCAN_QUALITY),
        pil_to_jpeg(thumb, config.THUMB_QUALITY)
    )
    sane.exit()
    return jsonify({'success': True, 'id': id})


@app.route('/img', defaults={'id': None})
@app.route('/img/<id>', methods=['GET', 'DELETE'])
def images(id):
    if not id:
        return jsonify(list(scanned_images.keys()))
    if id not in scanned_images:
        return jsonify({'success': False, 'error': f'Image with id {id} not found'}), 404
    if request.method == 'DELETE':
        scanned_images.pop(id, None)
        return jsonify({'success': True})

    resp = make_response(scanned_images[id][1 if 'thumb' in request.values else 0])
    resp.headers.set('Content-Type', 'image/jpeg')
    return resp

def make_filename(i: int):
    return 'scan_'+str(i+1).rjust(3,'0')+'.jpeg'

@app.route('/email', methods=['POST'])
def email():
    addr = request.values.get('email', None)
    if not addr:
        return jsonify({'success': False, 'error': 'No email address specified.'}), 400
    if not scanned_images:
        return jsonify({'success': False, 'error': 'There are no scanned images to send.'}), 400

    msg = MIMEMultipart()
    msg['From'] = config.MAIL_FROM
    msg['To'] = addr
    msg['Date'] = formatdate(localtime=True)
    msg['Subject'] = config.MAIL_SUBJECT
    msg.attach(MIMEText(config.MAIL_BODY))

    for i, img in enumerate(scanned_images.values()):
        filename = 'scan_'+str(i+1).rjust(3,'0')+'.jpeg'
        part = MIMEApplication(img[0], Name=filename)
        part['Content-Disposition'] = 'attachment; filename="{}"'.format(filename)
        msg.attach(part)

    conn = smtp(config.SMTP_SERVER)
    conn.set_debuglevel(False)
    conn.login(config.SMTP_USER, config.SMTP_PASSWORD)
    try:
        conn.sendmail(config.MAIL_FROM, addr, msg.as_string())
    except Exception as ex:
        return jsonify({'success': False, 'error': 'Failed sending email.'}), 500
    conn.quit()
    return jsonify({'success': True})

@app.route('/zip')
def zip():
    if not scanned_images:
        return redirect(url_for('index'))
    bio = BytesIO()
    with zipfile.ZipFile(bio, 'a') as z:
        for i, img in enumerate(scanned_images.values()):
            z.writestr(make_filename(i), img[0])

    bio.seek(0)
    return send_file(bio, attachment_filename='scan.zip', as_attachment=True)


@app.route('/')
def index():
    return render_template('index.html')

@entrypoint
def main(dbg=False, host='0.0.0.0'):
    app.run(debug=dbg, host=host)
