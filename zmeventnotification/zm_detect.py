#!/usr/bin/python3

# Main detection script that loads different detection models
# look at pyzm.ml for different detectors

from __future__ import division
import sys
#lets do this _after_ log init so we log it
#import cv2
import argparse
import datetime
import os
import numpy as np
import re
import imutils
import ssl
import pickle
import json
import time
import requests
import subprocess
import traceback
import ast 
# Modules that load cv2 will go later 
# so we can log misses
import pyzm.ZMLog as log 
import zmes_hook_helpers.utils as utils
import pyzm.helpers.utils as pyzmutils
import zmes_hook_helpers.common_params as g
from pyzm import __version__ as pyzm_version

auth_header = None

__app_version__ = '6.1.16'

def remote_detect(stream=None, options=None, api=None, args=None):
    # This uses mlapi (https://github.com/pliablepixels/mlapi) to run inferencing and converts format to what is required by the rest of the code.

    import requests
    import cv2
    
    bbox = []
    label = []
    conf = []
    model = 'object'
    files={}
    api_url = g.config['ml_gateway']
    g.logger.Info('Detecting using remote API Gateway {}'.format(api_url))
    login_url = api_url + '/login'
    object_url = api_url + '/detect/object?type='+model
    access_token = None
    global auth_header

    data_file = g.config['base_data_path'] + '/zm_login.json'
    if os.path.exists(data_file):
        g.logger.Debug(2,'Found token file, checking if token has not expired')
        with open(data_file) as json_file:
            try:
                data = json.load(json_file)
                json_file.close()
            except Exception as e: 
                g.logger.Error ('Error loading login.json: {}'.format(e))
                os.remove(data_file)
                access_token = None
            else:
                generated = data['time']
                expires = data['expires']
                access_token = data['token']
                now = time.time()
                # lets make sure there is at least 30 secs left
                if int(now + 30 - generated) >= expires:
                    g.logger.Debug(
                        1,'Found access token, but it has expired (or is about to expire)'
                    )
                    access_token = None
                else:
                    g.logger.Debug(1,'Access token is valid for {} more seconds'.format(
                        int(now - generated)))
                    # Get API access token
    if not access_token:
        g.logger.Debug(1,'Invoking remote API login')
        r = requests.post(url=login_url,
                          data=json.dumps({
                              'username': g.config['ml_user'],
                              'password': g.config['ml_password'],
                             

                          }),
                          headers={'content-type': 'application/json'})
        data = r.json()
        access_token = data.get('access_token')
        if not access_token:
            raise ValueError('Error getting remote API token {}'.format(data))
            return
        g.logger.Debug(2,'Writing new token for future use')
        with open(data_file, 'w') as json_file:
            wdata = {
                'token': access_token,
                'expires': data.get('expires'),
                'time': time.time()
            }
            json.dump(wdata, json_file)
            json_file.close()

    auth_header = {'Authorization': 'Bearer ' + access_token}
    
    params = {'delete': True, 'response_format': 'zm_detect'}

    if args.get('file'):
        g.logger.Debug (2, "Reading image from {}".format(args.get('file')))
        image = cv2.imread(args.get('file'))
        if g.config['resize'] and g.config['resize'] != 'no':
            g.logger.Debug (2,'Resizing image before sending')
            img_new = imutils.resize(image,
                                     width=min(int(g.config['resize']),
                                               image.shape[1]))
            image = img_new
            ret, jpeg = cv2.imencode('.jpg', image)
            files = {'file': ('image.jpg', jpeg.tobytes())}

    else:
        files = {}
    #print (object_url)

    
    ml_overrides = {
        'model_sequence':g.config['ml_sequence'].get('general',{}).get('model_sequence'),
        'object': {
            'pattern': g.config['ml_sequence'].get('object',{}).get('general',{}).get('pattern')
        },
         'face': {
            'pattern': g.config['ml_sequence'].get('face',{}).get('general',{}).get('pattern')
        },
         'alpr': {
            'pattern': g.config['ml_sequence'].get('alpr',{}).get('general',{}).get('pattern')
        },
    }
    mid = args.get('monitorid')
    reason = args.get('reason')
    g.logger.Debug(2,f'Invoking mlapi with url:{object_url} and json: mid={mid} reason={reason} stream={stream}, stream_options={options} ml_overrides={ml_overrides} headers={auth_header} params={params} ')
    start = datetime.datetime.now()
    try:
        r = requests.post(url=object_url,
                        headers=auth_header,
                        params=params,
                        files=files,
                        json = {
                            'version': __app_version__, 
                            'mid': mid,
                            'reason': reason,
                            'stream': stream,
                            'stream_options':options,
                            'ml_overrides':ml_overrides
                        }
                        )
        r.raise_for_status()
    except Exception as e:
        g.logger.Error ('Error during remote post: {}'.format(str(e)))
        g.logger.Debug(2,traceback.format_exc())
        raise

    diff_time = (datetime.datetime.now() - start)
    g.logger.Debug(1,'remote detection inferencing took: {}'.format(diff_time))
    data = r.json()
    #print(r)
    matched_data = data['matched_data']
    if g.config['write_image_to_zm'] == 'yes'  and matched_data['frame_id']:
        url = '{}/index.php?view=image&eid={}&fid={}'.format(g.config['portal'], stream,matched_data['frame_id'] )
        g.logger.Debug(2,'Grabbing image from {} as we need to write objdetect.jpg'.format(url))
        try:
            response = api._make_request(url=url,  type='get')
            img = np.asarray(bytearray(response.content), dtype='uint8')
            img = cv2.imdecode (img, cv2.IMREAD_COLOR)
            if options.get('resize') and options.get('resize') != 'no':
                img = imutils.resize(img,width=options.get('resize'))
            matched_data['image'] = img
        except Exception as e:
            g.logger.Error ('Error during image grab: {}'.format(str(e)))
            g.logger.Debug(2,traceback.format_exc())
    return data['matched_data'], data['all_matches']


def append_suffix(filename, token):
    f, e = os.path.splitext(filename)
    if not e:
        e = '.jpg'
    return f + token + e


# main handler

def main_handler():
    # set up logging to syslog
    # construct the argument parse and parse the arguments
  
    ap = argparse.ArgumentParser()
    ap.add_argument('-c', '--config', help='config file with path')
    ap.add_argument('-e', '--eventid', help='event ID to retrieve')
    ap.add_argument('-p',
                    '--eventpath',
                    help='path to store object image file',
                    default='')
    ap.add_argument('-m', '--monitorid', help='monitor id - needed for mask')
    ap.add_argument('-v',
                    '--version',
                    help='print version and quit',
                    action='store_true')
    ap.add_argument(
                    '--bareversion',
                    help='print only app version and quit',
                    action='store_true')

    ap.add_argument('-o', '--output-path',
                    help='internal testing use only - path for debug images to be written')

    ap.add_argument('-f',
                    '--file',
                    help='internal testing use only - skips event download')


    ap.add_argument('-r', '--reason', help='reason for event (notes field in ZM)')

    ap.add_argument('-n', '--notes', help='updates notes field in ZM with detections', action='store_true')
    ap.add_argument('-d', '--debug', help='enables debug on console', action='store_true')

    args, u = ap.parse_known_args()
    args = vars(args)

    if args.get('version'):
        print('app:{}, pyzm:{}'.format(__app_version__,pyzm_version))
        exit(0)

    if args.get('bareversion'):
        print('{}'.format(__app_version__))
        exit(0)

    if not args.get('config'):
        print ('--config required')
        exit(1)

    if not args.get('file')and not args.get('eventid'):
        print ('--eventid required')
        exit(1)

    utils.get_pyzm_config(args)

    if args.get('debug'):
        g.config['pyzm_overrides']['dump_console'] = True
        g.config['pyzm_overrides']['log_debug'] = True
        g.config['pyzm_overrides']['log_level_debug'] = 5
        g.config['pyzm_overrides']['log_debug_target'] = None

    if args.get('monitorid'):
        log.init(name='zmesdetect_' + 'm' + args.get('monitorid'), override=g.config['pyzm_overrides'])
    else:
        log.init(name='zmesdetect',override=g.config['pyzm_overrides'])
    g.logger = log
    
    es_version='(?)'
    try:
        es_version=subprocess.check_output(['/usr/bin/zmeventnotification.pl', '--version']).decode('ascii')
    except:
        pass


    try:
        import cv2
    except ImportError as e:
        g.logger.Fatal (f'{e}: You might not have installed OpenCV as per install instructions. Remember, it is NOT automatically installed')

    g.logger.Info('---------| app:{}, pyzm:{}, ES:{} , OpenCV:{}|------------'.format(__app_version__,pyzm_version, es_version, cv2.__version__))
   

    
    # load modules that depend on cv2
    try:
        import zmes_hook_helpers.image_manip as img
    except Exception as e:
        g.logger.Error (f'{e}')
        exit(1)
    g.polygons = []

    # process config file
    g.ctx = ssl.create_default_context()
    utils.process_config(args, g.ctx)


    # misc came later, so lets be safe
    if not os.path.exists(g.config['base_data_path'] + '/misc/'):
        try:
            os.makedirs(g.config['base_data_path'] + '/misc/')
        except FileExistsError:
            pass  # if two detects run together with a race here

    if not g.config['ml_gateway']:
        g.logger.Info('Importing local classes for Object/Face')
        import pyzm.ml.object as object_detection
       
    else:
        g.logger.Info('Importing remote shim classes for Object/Face')
        from zmes_hook_helpers.apigw import ObjectRemote, FaceRemote, AlprRemote
    # now download image(s)


    start = datetime.datetime.now()

    obj_json = []

    import pyzm.api as zmapi
    api_options  = {
    'apiurl': g.config['api_portal'],
    'portalurl': g.config['portal'],
    'user': g.config['user'],
    'password': g.config['password'] ,
    'logger': g.logger, # use none if you don't want to log to ZM,
    'disable_ssl_cert_check': False if g.config['allow_self_signed']=='no' else True
    }

    g.logger.Info('Connecting with ZM APIs')
    zmapi = zmapi.ZMApi(options=api_options)
    stream = args.get('eventid') or args.get('file')
    ml_options = {}
    stream_options={}
    secrets = None 
    
    if g.config['ml_sequence'] and g.config['use_sequence'] == 'yes':
        g.logger.Debug(2,'using ml_sequence')
        ml_options = g.config['ml_sequence']
        secrets = pyzmutils.read_config(g.config['secrets'])
        ml_options = pyzmutils.template_fill(input_str=ml_options, config=None, secrets=secrets._sections.get('secrets'))
        ml_options = ast.literal_eval(ml_options)
        g.config['ml_sequence'] = ml_options
    else:
        g.logger.Debug(2,'mapping legacy ml data from config')
        ml_options = utils.convert_config_to_ml_sequence()
        g.config['ml_sequence'] = ml_options

    if g.config['stream_sequence'] and g.config['use_sequence'] == 'yes': # new sequence
        g.logger.Debug(2,'using stream_sequence')
        stream_options = g.config['stream_sequence']
        stream_options = ast.literal_eval(stream_options)
    else: # legacy
        g.logger.Debug(2,'mapping legacy stream data from config')
        if g.config['detection_mode'] == 'all':
            g.config['detection_mode'] = 'most_models'
        frame_set = g.config['frame_id']
        if g.config['frame_id'] == 'bestmatch':
            if g.config['bestmatch_order'] == 's,a':
                frame_set = 'snapshot,alarm'
            else:
                frame_set = 'alarm,snapshot'
        stream_options['resize'] =int(g.config['resize']) if g.config['resize'] != 'no' else None

        stream_options['strategy'] = g.config['detection_mode'] 
        stream_options['frame_set'] = frame_set       
        stream_options['disable_ssl_cert_check'] =  False if g.config['allow_self_signed']=='no' else True


    # These are stream options that need to be set outside of supplied configs         
    stream_options['api'] = zmapi
    stream_options['polygons'] = g.polygons
    g.config['stream_sequence'] = stream_options


    '''
    stream_options = {
            'api': zmapi,
            'download': False,
            'frame_set': frame_set,
            'strategy': g.config['detection_mode'],
            'polygons': g.polygons,
            'resize': int(g.config['resize']) if g.config['resize'] != 'no' else None

    }
    '''

   
    m = None
    matched_data = None
    all_data = None


    if not args['file'] and int(g.config['wait']) > 0:
        g.logger.Info('Sleeping for {} seconds before inferencing'.format(
            g.config['wait']))
        time.sleep(g.config['wait'])

    if g.config['ml_gateway']:
        stream_options['api'] = None
        stream_options['monitorid'] = args.get('monitorid')
        start = datetime.datetime.now()
        try:
            matched_data,all_data = remote_detect(stream=stream, options=stream_options, api=zmapi, args=args)
            diff_time = (datetime.datetime.now() - start)
            g.logger.Debug(1,'Total remote detection detection took: {}'.format(diff_time))
        except Exception as e:
            g.logger.Error ("Error with remote mlapi:{}".format(e))
            g.logger.Debug(2,traceback.format_exc())

            if g.config['ml_fallback_local'] == 'yes':
                g.logger.Debug (1, "Falling back to local detection")
                stream_options['api'] = zmapi
                from pyzm.ml.detect_sequence import DetectSequence
                m = DetectSequence(options=ml_options, logger=g.logger)
                matched_data,all_data = m.detect_stream(stream=stream, options=stream_options)
    

    else:
        from pyzm.ml.detect_sequence import DetectSequence
        m = DetectSequence(options=ml_options, logger=g.logger)
        matched_data,all_data = m.detect_stream(stream=stream, options=stream_options)
    


    #print(f'ALL FRAMES: {all_data}\n\n')
    #print (f"SELECTED FRAME {matched_data['frame_id']}, size {matched_data['image_dimensions']} with LABELS {matched_data['labels']} {matched_data['boxes']} {matched_data['confidences']}")
    #print (matched_data)
    '''
     matched_data = {
            'boxes': matched_b,
            'labels': matched_l,
            'confidences': matched_c,
            'frame_id': matched_frame_id,
            'image_dimensions': self.media.image_dimensions(),
            'image': matched_frame_img
        }
    '''

    # let's remove past detections first, if enabled 
    if g.config['match_past_detections'] == 'yes' and args.get('monitorid'):
        # point detections to post processed data set
        g.logger.Info('Removing matches to past detections')
        bbox_t, label_t, conf_t = img.processPastDetection(
            matched_data['boxes'], matched_data['labels'], matched_data['confidences'], args.get('monitorid'))
        # save current objects for future comparisons
        g.logger.Debug(1,
            'Saving detections for monitor {} for future match'.format(
                args.get('monitorid')))
        try:
            mon_file = g.config['image_path'] + '/monitor-' + args.get(
            'monitorid') + '-data.pkl'
            f = open(mon_file, "wb")
            pickle.dump(matched_data['boxes'], f)
            pickle.dump(matched_data['labels'], f)
            pickle.dump(matched_data['confidences'], f)
            f.close()
        except Exception as e:
            g.logger.Error(f'Error writing to {mon_file}, past detections not recorded:{e}')

        matched_data['boxes'] = bbox_t
        matched_data['labels'] = label_t
        matched_data['confidences'] = conf_t

    obj_json = {
        'labels': matched_data['labels'],
        'boxes': matched_data['boxes'],
        'frame_id': matched_data['frame_id'],
        'confidences': matched_data['confidences'],
        'image_dimensions': matched_data['image_dimensions']
    }

    # 'confidences': ["{:.2f}%".format(item * 100) for item in matched_data['confidences']],
    
    detections = []
    seen = {}
    pred=''
    prefix = ''

    if matched_data['frame_id'] == 'snapshot':
        prefix = '[s] '
    elif matched_data['frame_id'] == 'alarm':
        prefix = '[a] '
    else:
        prefix = '[x] '
        #g.logger.Debug (1,'CONFIDENCE ARRAY:{}'.format(conf))
    for idx, l in enumerate(matched_data['labels']):
        if l not in seen:
            if g.config['show_percent'] == 'no':
                pred = pred + l + ','
            else:
                pred = pred + l + ':{:.0%}'.format(matched_data['confidences'][idx]) + ' '
            seen[l] = 1

    if pred != '':
        pred = pred.rstrip(',')
        pred = prefix + 'detected:' + pred
        g.logger.Info('Prediction string:{}'.format(pred))
        jos = json.dumps(obj_json)
        g.logger.Debug(1,'Prediction string JSON:{}'.format(jos))
        print(pred + '--SPLIT--' + jos)

        if (matched_data['image'] is not None) and (g.config['write_image_to_zm'] == 'yes' or g.config['write_debug_image'] == 'yes'):
            #print (f'********* REMOTE POLY: {remote_polygons}')
            debug_image = pyzmutils.draw_bbox(image=matched_data['image'],boxes=matched_data['boxes'], 
                                              labels=matched_data['labels'], confidences=matched_data['confidences'],
                                              polygons=matched_data['polygons'], poly_thickness = g.config['poly_thickness'],
                                              write_conf=True if g.config['show_percent'] == 'yes' else False )

            if g.config['write_debug_image'] == 'yes':
                for _b in matched_data['error_boxes']:
                    cv2.rectangle(debug_image, (_b[0], _b[1]), (_b[2], _b[3]),
                        (0,0,255), 1)
                filename_debug = g.config['image_path']+'/'+os.path.basename(append_suffix(stream, '-{}-debug'.format(matched_data['frame_id'])))
                g.logger.Debug (1,'Writing bound boxes to debug image: {}'.format(filename_debug))
                cv2.imwrite(filename_debug,debug_image)

            if g.config['write_image_to_zm'] == 'yes' and args.get('eventpath'):
                g.logger.Debug(1,'Writing detected image to {}/objdetect.jpg'.format(
                    args.get('eventpath')))
                cv2.imwrite(args.get('eventpath') + '/objdetect.jpg', debug_image)
                jf = args.get('eventpath')+ '/objects.json'
                g.logger.Debug(1,'Writing JSON output to {}'.format(jf))
                try:
                    with open(jf, 'w') as jo:
                        json.dump(obj_json, jo)
                        jo.close()
                except Exception as e:
                    g.logger.Error(f'Error creating {jf}:{e}')
                    
        if args.get('notes'):
            url = '{}/events/{}.json'.format(g.config['api_portal'], args['eventid'])
            try:
                ev = zmapi._make_request(url=url,  type='get')
            except Exception as e:
                g.logger.Error ('Error during event notes retrieval: {}'.format(str(e)))
                g.logger.Debug(2,traceback.format_exc())
                exit(0) # Let's continue with zmdetect

            new_notes = pred
            if ev.get('event',{}).get('Event',{}).get('Notes'): 
                old_notes = ev['event']['Event']['Notes']
                old_notes_split = old_notes.split('Motion:')
                old_d = old_notes_split[0] # old detection
                try:
                    old_m = old_notes_split[1] 
                except IndexError:
                    old_m = ''
                new_notes = pred + 'Motion:'+ old_m
                g.logger.Debug (1,'Replacing old note:{} with new note:{}'.format(old_notes, new_notes))
                

            payload = {}
            payload['Event[Notes]'] = new_notes
            try:
                ev = zmapi._make_request(url=url, payload=payload, type='put')
            except Exception as e:
                g.logger.Error ('Error during notes update: {}'.format(str(e)))
                g.logger.Debug(2,traceback.format_exc())

        if g.config['create_animation'] == 'yes':
            if not args.get('eventid'):
                g.logger.Error ('Cannot create animation as you did not pass an event ID')
            else:
                g.logger.Debug(1,'animation: Creating burst...')
                try:
                    img.createAnimation(matched_data['frame_id'], args.get('eventid'), args.get('eventpath')+'/objdetect', g.config['animation_types'])
                except Exception as e:
                    g.logger.Error('Error creating animation:{}'.format(e))
                    g.logger.Error('animation: Traceback:{}'.format(traceback.format_exc()))
                
            

if __name__ == '__main__':
    try:
        main_handler()
        g.logger.Debug (1, "Closing logs")
        g.logger.close()
    except Exception as e:
        if g.logger:
            g.logger.Fatal('Unrecoverable error:{} Traceback:{}'.format(e,traceback.format_exc()))
            g.logger.Debug (1, "Closing logs")
            g.logger.close()
        else:
            print('Unrecoverable error:{} Traceback:{}'.format(e,traceback.format_exc())) 
        exit(1)