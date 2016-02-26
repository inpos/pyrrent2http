#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import libtorrent as lt
import logging
import sys, os
from random import SystemRandom
from time import time as time_time
import urlparse, urllib
import platform
import BaseHTTPServer
import SocketServer
import threading

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
#############################################################

class TorrentFS(object):
    def __init__(self, handle, startIndex):
        self.tfs = AttributeDict()
        

#############################################################

class AttributeDict(dict):
    def __getattr__(self, attr):
        return self[attr]
    def __setattr__(self, attr, value):
        self[attr] = value

VERSION = "0.0.1"
USER_AGENT = "pyrrent2http/"+VERSION+" libtorrent/"+lt.version

class BoolArg(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        print(repr(values))
        if values is None: v = True
        elif values.lower() == 'true': v = True
        elif values.lower() == 'false': v = False
        setattr(namespace, self.dest, v)

def HttpHandlerFactory(root_obj):
    class HttpHandler(BaseHTTPServer.BaseHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super(HttpHandler, self).__init__(*args, **kwargs)
            self.root = root_obj
        def do_GET(self):
            self.send_response(200)
            if self.path == '/status':
                self.statusHandler()
            elif self.path == '/ls':
                self.lsHandler()
            elif self.path == '/peers':
                self.peersHandler()
            elif self.path == '/trackers':
                self.trackersHandler()
            elif self.path.startwith('/get/'):
                self.getHandler()
            elif self.path == '/shutdown':
                self.root.forceShutdown = True
                self.end_headers()
                self.wfile.write('OK')
            elif self.path.startwith('/files/'):
                self.filesHandler()
            else:
                self.end_headers()
                self.wfile.write('NONE')

        def statusHandler(self):
            self.send_header("Content-type", "application/json")
            self.end_headers()
            torrentHandle = self.root.torrentHandle
            tstatus = torrentHandle.status()
            status = {
                         'Name'           :   torrentHandle.name(),
                         'State'          :   int(tstatus.state),
                         'StateStr'       :   str(tstatus.state),
                         'Error'          :   tstatus.error,
                         'Progress'       :   tstatus.progress,
                         'DownloadRate'   :   tstatus.download_rate,
                         'UploadRate'     :   tstatus.upload_rate,
                         'TotalDownload'  :   tstatus.total_download,
                         'TotalUpload'    :   tstatus.total_upload,
                         'NumPeers'       :   tstatus.num_peers,
                         'NumSeeds'       :   tstatus.num_seeds,
                         'TotalSeeds'     :   tstatus.num_complete,
                         'TotalPeers'     :   tstatus.num_incomplete
                         }
            output = json.dumps(status)
            self.wfile.write(output)
        def lsHandler(self):
            self.send_header("Content-type", "application/json")
            self.end_headers()
            retFiles = list()
            torrentHandle = self.root.torrentHandle
            torrent_info = torrentHandle.get_torrent_info()
            if torrentHandle.is_valid():
                files = torrent_info.files()
                for n, file_ in enumerate(files):
                    Name = file.path
                    Size = file.size
                    Offset = file.offset
                    Download = torrentHandle.file_progress()[n]
                    Progress = Download / Size
                    SavePath = os.path.join(os.path.abspath(self.root.config.downloadPath), Name)
                    Url = 'http://' + self.root.config.bindAddress + '/files/' + Name
                    
                    fi = {
                          'Name':       Name,
                          'Size':       Size,
                          'Offset':     Offset,
                          'Download':   Download,
                          'Progress':   Progress,
                          'SavePath':   SavePath,
                          'Url':        Url
                          }
                    retFiles.append(fi)
            output = json.dumps(retFiles)
            self.wfile.write(output)
        def peersHandler(self):
            self.send_header("Content-type", "application/json")
            self.end_headers()
            torrentHandle = self.root.torrentHandle
            ret = list()
            for peer in torrentHandle.get_peer_info():
                if peer.flags & peer.connecting or peer.flags & peer.handshake:
                    continue
                pi = {
                       'Ip':            peer.ip,
                       'Flags':         peer.flags,
                       'Source':        peer.ip,            # ???
                       'UpSpeed':       peer.up_speed/1024,
                       'DownSpeed':     peer.down_speed/1024,
                       'TotalDownload': peer.total_download,
                       'TotalUpload':   peer.total_upload,
                       'Country':       peer.ip,  # ???
                       'Client':        peer.client
                       }
                ret.append(pi)
            output = json.dumps(ret)
            self.wfile.write(output)

class Pyrrent2http(object):
    def parseFlags(self):
        parser = argparse.ArgumentParser(add_help=True, version=VERSION)
        parser.add_argument('--uri', type=str, default='', help='Magnet URI or .torrent file URL', dest='uri')
        parser.add_argument('--bind', type=str, default='localhost:5001', help='Bind address of torrent2http', dest='bindAddress')
        parser.add_argument('--dl-path', type=str, default='.', help='Download path', dest='downloadPath')
        parser.add_argument('--max-idle', type=int, default=-1, help='Automatically shutdown if no connection are active after a timeout', dest='idleTimeout')
        parser.add_argument('--file-index', type=int, default=-1, help='Start downloading file with specified index immediately (or start in paused state otherwise)', dest='fileIndex')
        parser.add_argument('--keep-complete', nargs='?', action=BoolArg, default=False, help='Keep complete files after exiting', dest='keepComplete', choices=('true', 'false'))
        parser.add_argument('--keep-incomplete', nargs='?', action=BoolArg, default=False, help='Keep incomplete files after exiting', dest='keepIncomplete', choices=('true', 'false'))
        parser.add_argument('--keep-files', nargs='?', action=BoolArg, default=False, help='Keep all files after exiting (incl. -keep-complete and -keep-incomplete)', dest='keepFiles', choices=('true', 'false'))
        parser.add_argument('--show-stats', nargs='?', action=BoolArg, default=False, help='Show all stats (incl. -overall-progress -files-progress -pieces-progress)', dest='showAllStats', choices=('true', 'false'))
        parser.add_argument('--overall-progress', nargs='?', action=BoolArg, default=False, help='Show overall progress', dest='showOverallProgress', choices=('true', 'false'))
        parser.add_argument('--files-progress', nargs='?', action=BoolArg, default=False, help='Show files progress', dest='showFilesProgress', choices=('true', 'false'))
        parser.add_argument('--pieces-progress', nargs='?', action=BoolArg, default=False, help='Show pieces progress', dest='showPiecesProgress', choices=('true', 'false'))
        parser.add_argument('--debug-alerts', nargs='?', action=BoolArg, default=False, help='Show debug alert notifications', dest='debugAlerts', choices=('true', 'false'))
        parser.add_argument('--exit-on-finish', nargs='?', action=BoolArg, default=False, help='Exit when download finished', dest='exitOnFinish', choices=('true', 'false'))
        parser.add_argument('--resume-file', type=str, default='', help='Use fast resume file', dest='resumeFile')
        parser.add_argument('--state-file', type=str, default='', help='Use file for saving/restoring session state', dest='stateFile')
        parser.add_argument('--user-agent', type=str, default=USER_AGENT, help='Set an user agent', dest='userAgent')
        parser.add_argument('--dht-routers', type=str, default='', help='Additional DHT routers (comma-separated host:port pairs)', dest='dhtRouters')
        parser.add_argument('--trackers', type=str, default='', help='Additional trackers (comma-separated URLs)', dest='trackers')
        parser.add_argument('--listen-port', type=int, default=6881, help='Use specified port for incoming connections', dest='listenPort')
        parser.add_argument('--torrent-connect-boost', type=int, default=50, help='The number of peers to try to connect to immediately when the first tracker response is received for a torrent', dest='torrentConnectBoost')
        parser.add_argument('--connection-speed', type=int, default=50, help='The number of peer connection attempts that are made per second', dest='connectionSpeed')
        parser.add_argument('--peer-connect-timeout', type=int, default=15, help='The number of seconds to wait after a connection attempt is initiated to a peer', dest='peerConnectTimeout')
        parser.add_argument('--request-timeout', type=int, default=20, help='The number of seconds until the current front piece request will time out', dest='requestTimeout')
        parser.add_argument('--dl-rate', type=int, default=-1, help='Max download rate (kB/s)', dest='maxDownloadRate')
        parser.add_argument('--ul-rate', type=int, default=-1, help='Max upload rate (kB/s)', dest='maxUploadRate')
        parser.add_argument('--connections-limit', type=int, default=200, help='Set a global limit on the number of connections opened', dest='connectionsLimit')
        parser.add_argument('--encryption', type=int, default=1, help='Encryption: 0=forced 1=enabled (default) 2=disabled', dest='encryption')
        parser.add_argument('--min-reconnect-time', type=int, default=60, help='The time to wait between peer connection attempts. If the peer fails, the time is multiplied by fail counter', dest='minReconnectTime')
        parser.add_argument('--max-failcount', type=int, default=3, help='The maximum times we try to connect to a peer before stop connecting again', dest='maxFailCount')
        parser.add_argument('--no-sparse', nargs='?', action=BoolArg, default=False, help='Do not use sparse file allocation', dest='noSparseFile', choices=('true', 'false'))
        parser.add_argument('--random-port', nargs='?', action=BoolArg, default=False, help='Use random listen port (49152-65535)', dest='randomPort', choices=('true', 'false'))
        parser.add_argument('--enable-scrape', nargs='?', action=BoolArg, default=False, help='Enable sending scrape request to tracker (updates total peers/seeds count)', dest='enableScrape', choices=('true', 'false'))
        parser.add_argument('--enable-dht', nargs='?', action=BoolArg, default=True, help='Enable DHT (Distributed Hash Table)', dest='enableDHT', choices=('true', 'false'))
        parser.add_argument('--enable-lsd', nargs='?', action=BoolArg, default=True, help='Enable LSD (Local Service Discovery)', dest='enableLSD', choices=('true', 'false'))
        parser.add_argument('--enable-upnp', nargs='?', action=BoolArg, default=True, help='Enable UPnP (UPnP port-mapping)', dest='enableUPNP', choices=('true', 'false'))
        parser.add_argument('--enable-natpmp', nargs='?', action=BoolArg, default=True, help='Enable NATPMP (NAT port-mapping)', dest='enableNATPMP', choices=('true', 'false'))
        parser.add_argument('--enable-utp', nargs='?', action=BoolArg, default=True, help='Enable uTP protocol', dest='enableUTP', choices=('true', 'false'))
        parser.add_argument('--enable-tcp', nargs='?', action=BoolArg, default=True, help='Enable TCP protocol', dest='enableTCP', choices=('true', 'false'))
        self.config = parser.parse_args()
        if self.config.uri == '':
            parser.print_usage()
            sys.exit(1)
        if self.config.resumeFile != '' and not self.config.keepFiles:
            logging.error('Usage of option --resume-file is allowed only along with --keep-files')
            sys.exit(1)
    
    def buildTorrentParams(self, uri):
        fileUri = urlparse.urlparse(uri)
        torrentParams = {}
        
        if fileUri.scheme == 'file':
            uriPath = fileUri.path
            if uriPath != '' and platform.system().lower() == 'windows' and os.path.sep == uriPath[0]:
                uriPath = uriPath[1:]
            try:
                absPath = os.path.abspath(uriPath)
                logging.info('Opening local file: %s', absPath)
                with open(absPath, 'rb') as f:
                    torrent_info = lt.torrent_info(lt.bdecode(f.read()))
            except Exception as e:
                errno, strerror = e.args
                logging.error(strerror)
                sys.exit(errno)
        else:
            logging.info('Will fetch: %s', uri)
            try:
                torrent_raw = urllib.urlopen(uri).read()
                torrent_info = lt.torrent_info(torrent_raw, len(torrent_raw))
            except Exception as e:
                errno, strerror = e.args
                logging.error(strerror)
                sys.exit(errno)
        torrentParams['ti'] = torrent_info
        logging.info('Setting save path: %s', self.config.downloadPath)
        torrentParams['save_path'] = self.config.downloadPath
        
        if os.path.exists(self.config.resumeFile):
            logging.info('Loading resume file: %s', self.config.resumeFile)
            try:
                with open(self.config.resumeFile, 'rb') as f:
                    torrentParams['resume_data'] = lt.bencode(f.read())
            except Exception as e:
                _, strerror = e.args
                logging.error(strerror)
        if self.config.noSparseFile:
            logging.info('Disabling sparse file support...')
            torrentParams["storage_mode"] = lt.storage_mode_t.storage_mode_allocate
        return torrentParams
    
    def addTorrent(self):
        torrentParams = self.buildTorrentParams(self.config.uri)
        logging.info('Adding torrent')
        self.torrentHandle = self.session.add_torrent(torrentParams)
        if self.config.trackers != '':
            trackers    = self.config.trackers.split(',')
            startTier   = 256 - len(trackers)
            for n in range(len(trackers)):
                tracker = trackers[n].strip()
                logging.info('Adding tracker: %s', tracker)
                self.torrentHandle.add_tracker(tracker, startTier + n)
        if self.config.enableScrape:
            logging.info('Sending scrape request to tracker')
            self.torrentHandle.scrape_tracker()
        logging.info('Downloading torrent: %s', torrentHandle.get_torrent_info().name())
    
    def startHTTP(self):
        logging.info('Starting HTTP Server...')
        handler = HttpHandlerFactory(self)
        # if config.idleTimeout > 0 {
        #     connTrackChannel := make(chan int, 10)
        #     handler = NewConnectionCounterHandler(connTrackChannel, mux)
        #     go inactiveAutoShutdown(connTrackChannel)
        # }
        logging.info('Listening HTTP on %s...\n', self.config.bindAddress)
        s = BaseHTTPServer.HTTPServer(tuple(self.config.bindAddress.split(':')), handler)
        # FIXME возможно, надо будет обрабатывать запросы в общем цикле.
        self.httpListener = threading.Thread(target = server.serve_forever)
        self.httpListener.start()
    
    def startServices(self):
        if self.config.enableDHT:
            logging.info('Starting DHT...')
            self.session.start_dht()
        if self.config.enableLSD:
            logging.info('Starting LSD...')
            self.session.start_lsd()
        if self.config.enableUPNP:
            logging.info('Starting UPNP...')
            self.session.start_upnp()
        if self.config.enableNATPMP:
            logging.info('Starting NATPMP...')
            self.session.start_natpmp()
    
    def startSession(self):
        logging.info('Starting session...')
        self.session = lt.session(lt.fingerprint('LT', lt.version_major, lt.version_minor, 0, 0),
                             flags=int(lt.session_flags_t.add_default_plugins))
        alertMask = (lt.alert.category_t.error_notification | 
                     lt.alert.category_t.storage_notification | 
                     lt.alert.category_t.tracker_notification |
                     lt.alert.category_t.status_notification)
        if self.config.debugAlerts:
            alertMask |= lt.alert.category_t.debug_notification
        self.session.set_alert_mask(alertMask)
        
        settings = self.session.settings()
        settings.request_timeout = self.config.requestTimeout
        settings.peer_connect_timeout = self.config.peerConnectTimeout
        settings.announce_to_all_trackers = True
        settings.announce_to_all_tiers = True
        settings.torrent_connect_boost = self.config.torrentConnectBoost
        settings.connection_speed = self.config.connectionSpeed
        settings.min_reconnect_time = self.config.minReconnectTime
        settings.max_failcount = self.config.maxFailCount
        settings.recv_socket_buffer_size = 1024 * 1024
        settings.send_socket_buffer_size = 1024 * 1024
        settings.rate_limit_ip_overhead = True
        settings.min_announce_interval = 60
        settings.tracker_backoff = 0
        self.session.set_settings(settings)
    
        if self.config.stateFile != '':
            logging.info('Loading session state from %s', self.config.stateFile)
            try:
                with open(self.config.stateFile, 'rb') as f:
                    bytes__ = f.read()
            except IOError as e:
                _, strerror = e.args
                logging.error(strerror)
            else:
                self.session.load_state(lt.bdecode(bytes__))
        
        rand = SystemRandom(time_time())
        portLower = self.config.listenPort
        if self.config.randomPort:
            portLower = rand.randint(0, 16374) + 49151
        portUpper = portLower + 10
        try:
            self.session.listen_on(portLower, portUpper)
        except IOError as e:
            errno, strerror = e.args
            logging.error(strerror)
            sys.exit(errno)
        
        settings = self.session.settings()
        if self.config.userAgent != '':
            settings.user_agent = self.config.userAgent
        if self.config.connectionsLimit >= 0:
            settings.connections_limit = self.config.connectionsLimit
        if self.config.maxDownloadRate >= 0:
            settings.download_rate_limit = self.config.maxDownloadRate * 1024
        if self.config.maxUploadRate >= 0:
            settings.upload_rate_limit = self.config.maxUploadRate * 1024
        settings.enable_incoming_tcp = self.config.enableTCP
        settings.enable_outgoing_tcp = self.config.enableTCP
        settings.enable_incoming_utp = self.config.enableUTP
        settings.enable_outgoing_utp = self.config.enableUTP
        self.session.set_settings(settings)
        
        if self.config.dhtRouters != '':
            routers = self.config.dhtRouters.split(',')
            for router in routers:
                router = router.strip()
                if router != '':
                    hostPort = router.split(':')
                    host = hostPort[0].strip()
                    try:
                        port = len(hostPort) > 1 and int(hostPort[1].strip()) or 6881
                    except ValueError as e:
                        errno, strerror = e.args
                        logging.error(strerror)
                        sys.exit(errno)
                    self.session.add_dht_router(host, port)
                    logging.info('Added DHT router: %s:%d', host, port)
        logging.info('Setting encryption settings')
        encryptionSettings = lt.pe_settings()
        encryptionSettings.out_enc_policy = lt.enc_policy(self.config.encryption)
        encryptionSettings.in_enc_policy = lt.enc_policy(self.config.encryption)
        encryptionSettings.allowed_enc_level = lt.enc_level.both
        encryptionSettings.prefer_rc4 = True
        self.session.set_pe_settings(encryptionSettings)
    
if __name__ == '__main__':
    pyrrent2http = Pyrrent2http()
    pyrrent2http.parseFlags()

    pyrrent2http.startSession()
    pyrrent2http.startServices()
    pyrrent2http.addTorrent()

    pyrrent2http.startHTTP()
    loop()
    shutdown()