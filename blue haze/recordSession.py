#
# Blue Haze
# Record Session
# 26 Oct 2020
#
# Fabrizio Poltronieri - fabrizio.poltronieri@dmu.ac.uk
# Craig Vear - cvear@dmu.ac.uk
#

from PySide2.QtCore import Slot, QObject
from PySide2.QtWidgets import QMessageBox
from database import *
from playAudioTrack import PlayAudioTrack
from subprocess import Popen

import shortuuid
import time
import threading
import asyncio
import nest_asyncio

import modules.utils as utls
import modules.config as cfg
import modules.ui as ui
import modules.datastructures as datastr


def current_milli_time():
    return int(round(time.time() * 1000))


class RecordSession(QMessageBox, QObject):
    def __init__(self, parent=None):
        super().__init__()

        if parent is not None:
            self.result_stop_recording = utls.EmitSignal(parent, parent.action_record_stop_button)
            self.send_complete_session_name = utls.EmitSignal(parent, parent.get_complete_session_name)

        self.session = datastr.RecordSessionData()

        self.video_process = None

        self.backing_track_player = PlayAudioTrack(parent=self)

        self.database = None
        self.video_file_name = None
        self.last_time_stamp = 0

        self.bitalino = None

        self.hardware = utls.Hardware()

        self.body_parts_list = ['nose',
                                'neck',
                                'r_shoudler',
                                'r_elbow',
                                'r_wrist',
                                'l_shoudler',
                                'l_elbow',
                                'l_wrist',
                                'r_eye',
                                'l_eye',
                                'r_ear',
                                'l_ear']
        self.dict_keys = ['x',
                          'y',
                          'confidence']
        self.brainbit_eeg_labels = ['eeg-T3',
                                    'eeg-T4',
                                    'eeg-O1',
                                    'eeg-O2']

        self.thread_get_data = None
        self.GET_DATA_INTERVAL = cfg.BITALINO_BAUDRATE / 1000 # todo: this doesnt work correctly

        self.loop = None

        nest_asyncio.apply()

    def start_recording(self,
                        session_name,
                        video_audio_path,
                        video_source,
                        audio_interface,
                        back_track,
                        mic_volume):
        self.loop = asyncio.get_event_loop()

        self.session.id = shortuuid.uuid()
        self.session.date = time.strftime('%Y%m%d')
        self.session.name = '{}_{}_{}'.format(self.session.date,
                                              session_name,
                                              self.session.id)
        self.send_complete_session_name.emit_signal({'session_name': self.session.name})
        self.session.video_audio_path = video_audio_path
        self.session.video_source = video_source
        self.session.audio_interface = audio_interface
        self.session.mic_volume = mic_volume / 100

        self.video_recording()

        # play the backtrack
        backing_track_file = '{}{}'.format(cfg.ASSETS_BACKING_AUDIO_FOLDER, back_track)
        self.backing_track_player.setup_media(backing_track_file)
        self.backing_track_player.play()

        self.database = Database(self.session.id,
                                 self.session.name,
                                 self.session.mic_volume,
                                 self.video_file_name,
                                 back_track)

        self.thread_get_data = threading.Event()
        self.get_data(self.thread_get_data)

    def video_recording(self):
        # this is an ugly workaround
        # because QMediarecorder
        # doesn't work on Windows
        # keep an eye regularly on:
        # https://doc.qt.io/qt-5/qtmultimedia-windows.html
        # to see if it gets supported
        # on Windows
        self.video_file_name = '{}/{}.avi'.format(self.session.video_audio_path,
                                                  self.session.name)
        # see:
        # https://trac.ffmpeg.org/wiki/Capture/Webcam
        cmd = None

        # list video and audio devices on Windows:
        # https://trac.ffmpeg.org/wiki/DirectShow
        # ffmpeg -list_devices true -f dshow -i dummy
        cmd = ['ffmpeg',
               '-f', 'dshow',
               '-framerate', '30',
               '-i', 'video={}:audio={}'.format(self.session.video_source,
                                                self.session.audio_interface.deviceName()),
               '-q:v', '3',
               '-b:v', '2M',
               '-filter:a', 'volume={}'.format(self.session.mic_volume),
               self.video_file_name]
        self.video_process = Popen(cmd)

    def get_data(self, stop_thread_get_data, old_pos=-1):
        # backtrack play position
        backing_track_pos = self.backing_track_player.position()

        if backing_track_pos != old_pos:
            delta_time = self.delta_time(backing_track_pos)

            if cfg.HARDWARE:
                # skeleton data
                raw_skeleton_data = self.hardware.read_realsense()
                # parse raw skeleton data
                skeleton_data = self.skeleton_parse(raw_skeleton_data)
                utls.logger.debug('REALSENSE: {}'.format(skeleton_data))

                # brainbit data
                raw_brainbit_data = self.hardware.read_brainbit()
                # parse and label brainbit data
                brainbit_data = self.brainbit_parse(raw_brainbit_data)
                utls.logger.debug('BRAINBIT: {}'.format(brainbit_data))

                # bitalino data
                bitalino_data = self.hardware.read_bitalino()

                # slicing usable bitalino data and convert to list
                bitalino_data = bitalino_data[0, -1]
                bitalino_data = bitalino_data.tolist()
                utls.logger.debug('BITALINO: {}'.format(bitalino_data))
            else:
                bitalino_data = brainbit_data = skeleton_data = 'no hardware'

            # calc chorus of backing track
            chorus_id = self.which_chorus(backing_track_pos)

            # insert data in the database
            self.loop.run_until_complete(self.database.insert_document(delta_time=delta_time,
                                                                       backing_track_position=backing_track_pos,
                                                                       chorus_id=chorus_id,
                                                                       bitalino_data=bitalino_data,
                                                                       brainbit_data=brainbit_data,
                                                                       skeleton_data=skeleton_data))

        if not stop_thread_get_data.is_set():
            # call it again
            threading.Timer(interval=self.GET_DATA_INTERVAL,
                            function=self.get_data,
                            args=[self.thread_get_data, backing_track_pos]).start()

    def extract_audio_from_video(self):
        audio_file_name = self.video_file_name[:-3]+'wav'
        audio_file_name_mono = self.video_file_name[:-4]+'_m.wav'

        # stereo
        cmd = ['ffmpeg',
               '-i', self.video_file_name,
               '-ab', '160k',
               '-ac', '2',
               '-ar', '44100',
               '-vn', audio_file_name]

        Popen(cmd)

    def which_chorus(self, backing_track_pos):
        # simple logging of chorus/ loop ID
        # 0 = count in; 1 = tune; 2 = improv 1; 3 = improv 2; 4 = improv 3; 5 = recap tune; 999 = ending
        if backing_track_pos < 4000:
            return 0
        elif 4000 <= backing_track_pos < 68000:
            return 1
        elif 68000 <= backing_track_pos < 132000:
            return 2
        elif 132000 <= backing_track_pos < 196000:
            return 3
        elif 196000 <= backing_track_pos < 260000:
            return 4
        elif 260000 <= backing_track_pos < 324000:
            return 5
        elif backing_track_pos >= 324000:
            return 999

    def brainbit_parse(self, raw_brainbit_data):
        # setup dict for each parse
        brainbit_data = {}

        # parse only the fields we need from timestamp, eegt2, eegt4, eeg01, eeg02, X, X, X, X, X, X, boardID, battery
        for i, raw in enumerate(raw_brainbit_data[1:5]):
            for eeg in raw:
                brainbit_data[self.brainbit_eeg_labels[i]] = eeg

        return brainbit_data

    def skeleton_parse(self, raw_skeleton_data):
        if len(raw_skeleton_data) > 0:
            # create an array
            joint_coord_list_x = []
            joint_coord_list_y = []
            coord_conf_list = []

            for keypoint in raw_skeleton_data:

                # extract joint coords for 1st 8 & last 4 joints
                for joint in keypoint[0:1]:

                    # 1st 8
                    for coords in joint[:8]:
                        joint_coord_list_x.append(coords[0:1])
                        joint_coord_list_y.append(coords[1:2])

                    # last 4
                    for coords in joint[-4:]:
                        joint_coord_list_x.append(coords[0:1])
                        joint_coord_list_y.append(coords[1:2])

                # extract coord confidences for  1st 8 & last 4 joints
                for conf in keypoint[1:2]:

                    # 1st 8
                    for value in conf[:8]:
                        coord_conf_list.append(value)
                        # d1[dict_keys[2]] = value

                    # last 4
                    for value in conf[-4:]:
                        coord_conf_list.append(value)
                        # d1[dict_keys[2]] = value

            # make dicts
            skeleton_data = {}

            for d, joint in enumerate(self.body_parts_list):
                dict_data = {}
                dict_data[self.dict_keys[0]] = joint_coord_list_x[d][0]
                dict_data[self.dict_keys[1]] = joint_coord_list_y[d][0]
                dict_data[self.dict_keys[2]] = coord_conf_list[d]
                skeleton_data[joint] = dict_data

        else:
            skeleton_data = {'nose': {'x': -1, 'y': -1, 'confidence': 0},
                             'neck': {'x': -1, 'y': -1, 'confidence': 0},
                             'r_shoudler': {'x': -1, 'y': -1, 'confidence': 0},
                             'r_elbow': {'x': -1, 'y': -1, 'confidence': 0},
                             'r_wrist': {'x': -1, 'y': -1, 'confidence': 0},
                             'l_shoudler': {'x': -1, 'y': -1, 'confidence': 0},
                             'l_elbow': {'x': -1, 'y': -1, 'confidence': 0},
                             'l_wrist': {'x': -1, 'y': -1, 'confidence': 0},
                             'r_eye': {'x': -1, 'y': -1, 'confidence': 0},
                             'l_eye': {'x': -1, 'y': -1, 'confidence': 0},
                             'r_ear': {'x': -1, 'y': -1, 'confidence': 0},
                             'l_ear': {'x': -1, 'y': -1, 'confidence': 0}
                             }
        return skeleton_data

    def delta_time(self, now_time_stamp):
        self.now_time_stamp = now_time_stamp
        delta = self.now_time_stamp - self.last_time_stamp
        self.last_time_stamp = self.now_time_stamp
        return delta

    def stop(self):
        self.backing_track_player.stop()
        self.thread_get_data.set()
        self.video_process.terminate()
        self.database.close()
        self.hardware.stop()

    # slot to get signal when the back track file ends
    @Slot(dict)
    def player_track_end(self, status):
        if status['end_of_audio_file']:
            # # timer to send the stop signal
            # timer = QTimer(self)
            # timer.setInterval(3500)
            # timer.timeout.connect(self.back_track_end_after_three_seconds(timer))

            # show the message box warning about the end of the session in 3 seconds
            _ = ui.TimerMessageBox(3,
                                   'Blue Haze - Stopping session',
                                   'Stopping automatically the session in {} seconds',
                                   self).exec_()

            # send signal to main to stop the recording
            return_dict = {
                'stop_recording': True
            }
            self.result_stop_recording.emit_signal(return_dict)
