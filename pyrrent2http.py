#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import libtorrent as lt
import logging
import sys, os
from random import SystemRandom
import time
import urlparse, urllib
import platform
import BaseHTTPServer
import SocketServer
import threading
import signal
import io

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
######################################################################################
VERSION = "0.0.1"
USER_AGENT = "pyrrent2http/" + VERSION + " libtorrent/" + lt.version
######################################################################################

class Ticker(object):
    def __init__(self, interval):
        self.tick = False
        self._timer     = None
        self.interval   = interval
        self.is_running = False
        self.start()

    def true(self):
        if self.tick:
            self.tick = False
            return True
        else:
            return False

    def _run(self):
        self.is_running = False
        self.start()
        self.tick = True

    def start(self):
        if not self.is_running:
            self._timer = threading.Timer(self.interval, self._run)
            self._timer.start()
            self.is_running = True

    def stop(self):
        self._timer.cancel()
        self.is_running = False

#######################################################################################

class TorrentFile(object):
    tfs         =   None
    num         =   int()
    closed      =   False
    savePath    =   str()
    fileEntry   =   None
    index       =   int()
    filePtr     =   None
    downloaded  =   int()
    progress    =   float()
    def __init__(self, tfs, fileEntry, savePath, index):
        self.tfs = tfs
        self.fileEntry = fileEntry
        self.savePath = savePath
        self.index = index
    def SavePath(self):
        return self.savePath
    def Index(self):
        return tf.index
    def Downloaded(self):
        return self.downloaded
    def Progress(self):
        return self.progress
    def FilePtr(self):
        if self.closed:
            return None
        if self.filePtr is None:
            while not os.path.exists(self.savePath):
                time.sleep(0.1)
            self.filePtr = io.open(self.savePath, 'rb')
        return self.filePtr
    def log(self, message):
        fnum = self.num
        logging.info("[%d] %s\n" % (fnum, message))
    def Pieces(self):
        startPiece, _ = self.pieceFromOffset(1)
        endPiece, _ = self.pieceFromOffset(self.Size() - 1)
        return startPiece, endPiece
    def SetPriority(self, priority):
        self.tfs.setPriority(self.index, priority)
    def Stat(self):
        return self
    def readOffset(self):
        return self.filePtr.seek(0, os.SEEK_CUR)
    def havePiece(self, piece):
        return self.tfs.handle.have_piece(piece)
    def pieceLength(self):
        return self.tfs.info.piece_length()
    def pieceFromOffset(self, offset):
        pieceLength = int(self.pieceLength())
        piece = int((self.Offset() + offset) / pieceLength)
        pieceOffset = int((self.Offset() + offset) % pieceLength)
        return piece, pieceOffset
    def Offset(self):
        return self.fileEntry.offset
    def waitForPiece(self, piece):
        if not self.havePiece(piece):
            self.log('Waiting for piece %d' % (piece,))
            self.tfs.handle.set_piece_deadline(piece, 50)
        while not self.havePiece(piece):
            if self.tfs.handle.piece_priority(piece) == 0 or self.closed:
                return None
            time.sleep(0.1)
        _, endPiece = self.Pieces()
        if piece < endPiece and not self.havePiece(piece + 1):
            self.tfs.handle.set_piece_deadline(piece + 1, 100)
        return None
    def Close(self):
        if self.closed: return
        self.log('Closing %s...' % (self.Name(),))
        self.tfs.removeOpenedFile(self)
        self.closed = True
        if self.filePtr is not None:
            err = self.filePtr.close()
        return err
    def ShowPieces(self):
        pieces = self.tfs.handle.status().pieces
        startPiece, endPiece = self.Pieces()
        str_ = ''
        for i in range(startPiece, endPiece + 1):
            if pieces[i] == False:
                str_ += "-"
            else:
                str_ += "#"
        self.log(str_)
    def Read(self, buf):
        filePtr = self.FilePtr()
        if filePtr is None:
            return 0
        toRead = len(buf)
        if toRead > self.pieceLength():
            toRead = self.pieceLength()
        readOffset = self.readOffset()
        startPiece, _ = self.pieceFromOffset(readOffset)
        endPiece, _ = self.pieceFromOffset(readOffset + toRead)
        for i in range(startPiece,  endPiece + 1):
            if self.waitForPiece(i) is None:
                return 0
        read = filePtr.readinto(buf)
        return read
    def Seek(self, offset, whence):
        filePtr = self.FilePtr()
        if filePtr is None: return
        if whence == os.SEEK_END:
            offset = self.Size() - offset
            whence = os.SEEK_SET
        newOffset = filePtr.seek(offset, whence)
        self.log('Seeking to %d/%d' % (newOffset, self.Size()))
        return newOffset
    def Name(self):
        return self.fileEntry.path
    def Size(self):
        return self.fileEntry.size
    def IsComplete(self):
        return self.downloaded == self.Size()

#######################################################################################

class TorrentDir(object):
    tfs = None
    entriesRead = int()
    def __init__(self, tfs):
        self.tfs = tfs
    def Readdir(self, count):
        info = self.tfs.TorrentInfo()
        totalFiles = info.num_files()
        read = self.entriesRead
        toRead = totalFiles - read
        if count >= 0 and count < toRead:
            toRead = count
        files = [None for x in range(toRead)]
        for i in range(toRead):
            files[i] = self.tfs.FileAt(read)
            read += 1
        return files
        

#######################################################################################

class TorrentFS(object):
    handle      =       None
    info        =       None
    priorities  =       list()
    openedFiles =       list()
    lastOpenedFile =    None
    shuttingDown   =    False
    fileCounter =       int()
    progresses  =       list()

    def __init__(self, root, handle, startIndex):
        self.root = root
        self.handle = handle
        self.waitForMetadata()
        if startIndex < 0:
            logging.info('No -file-index specified, downloading will be paused until any file is requested')
        for i in range(self.TorrentInfo().num_files()):
            if startIndex == i:
                self.setPriority(i, 1)
            else:
                self.setPriority(i, 0)

    def Shutdown(self):
        self.shuttingDown = True
        if len(self.openedFiles) > 0:
            logging.info('Closing %d opened file(s)' % (len(self.openedFiles),))
            for f in self.openedFiles:
                f.Close()
    def LastOpenedFile(self):
        return self.LastOpenedFile  
    def addOpenedFile(self, file):
        self.openedFiles.append(file)    
    def setPriority(self, index, priority):
        if self.priorities[index] != priority:
            logging.info('Setting %s priority to %d', self.info.file_at(0).path, priority)
            self.priorities[index] = priority
            self.handle.file_priority(index, priority)
    def findOpenedFile(self, file):
        for i, f in enumerate(self.openedFiles):
            if f == file:
                return i
        return -1
    def removeOpenedFile(self, file):
        pos = self.findOpenedFile(file)
        if pos >= 0:
            del self.openedFiles[pos]
    def waitForMetadata(self):
        if not self.handle.status().has_metadata:
            time.sleep(0.1)
    def HasTorrentInfo(self):
        return self.info is not None
    def TorrentInfo(self):
        while not isinstance(self.info, lt.torrent_info):
            time.sleep(0.1)
        return self.info
    def LoadFileProgress(self):
        self.progresses = self.handle.file_progress()
    def getFileDownloadedBytes(self, i):
        try:
            bytes = self.progresses[i]
        except IndexError:
            bytes = 0
        return bytes
    def Files(self):
        info = self.TorrentInfo()
        files = [None for x in range(info.num_files())]
        for i in range(info.num_files()):
            file = self.FileAt(i)
            file.downloaded = self.getFileDownloadedBytes(i)
            if file.Size() > 0:
                file.progress = float(file.downloaded)/float(file.Size())
            files[i] = file
        return files
    def SavePath(self):
        return self.root.torrentParams['save_path']
    def FileAt(self, index):
        info = self.TorrentInfo()
        if index < 0 or index >= info.num_files():
            raise IndexError
        fileEntry = info.file_at(index)
        path = os.path.abspath(os.path.join(self.SavePath(), fileEntry.path))
        return TorrentFile(
                           self,
                           fileEntry,
                           path,
                           index
                           )
    def FileByName(self, name):
        savePath = os.path.abspath(os.path.join(self.SavePath(), name))
        for file in self.Files():
            if file.SavePath() == savePath:
                return file
        raise IOError
    def Open(self, name):
        if self.shuttingDown or not self.HasTorrentInfo():
            raise IOError
        if name == '/':
            return TorrentDir(self)
        return self.OpenFile(name)
    def checkPriorities(self):
        for index, priority in enumerate(self.priorities):
            if priority == 0:
                continue
            found = False
            for f in self.openedFiles:
                if f.index == index:
                    found = True
                    break
            if not found:
                self.setPriority(index, 0)
    def OpenFile(self, name):
        try:
            tf = FileByName(name)
        except IOError:
            return
        self.fileCounter += 1
        tf.num = self.fileCounter
        tf.log('Opening %s...' % (tf.Name(),))
        tf.SetPriority(1)
        startPiece, _ = tf.Pieces()
        self.handle.set_piece_deadline(startPiece, 50)
        self.lastOpenedFile = tf
        self.addOpenedFile(tf)
        self.checkPriorities()
        return tf
        
#############################################################

class AttributeDict(dict):
    def __getattr__(self, attr):
        return self[attr]
    def __setattr__(self, attr, value):
        self[attr] = value

class BoolArg(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        print(repr(values))
        if values is None: v = True
        elif values.lower() == 'true': v = True
        elif values.lower() == 'false': v = False
        setattr(namespace, self.dest, v)
        
class ThreadingHTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    pass

def HttpHandlerFactory(root_obj):
    class HttpHandler(BaseHTTPServer.BaseHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super(HttpHandler, self).__init__(*args, **kwargs)
            self.root = root_obj
        def do_GET(self):
            if self.path == '/status':
                self.statusHandler()
            elif self.path == '/ls':
                self.lsHandler()
            elif self.path == '/peers':
                self.peersHandler()
            elif self.path == '/trackers':
                self.trackersHandler()
            #elif self.path.startwith('/get/'):   # Неясно, зачем 
            #    self.getHandler()                # этот запрос?
            elif self.path == '/shutdown':
                self.root.forceShutdown = True
                self.end_headers()
                self.wfile.write('OK')
            elif self.path.startwith('/files/'):
                self.filesHandler()
            else:
                self.send_error(404, 'Not found')
                self.end_headers()

        def filesHandler(self):
            

        def statusHandler(self):
            self.send_response(200)
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
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            retFiles = list()
            if self.root.torrentFS.HasTorrentInfo():
                files = self.root.torrentFS.Files()
                for file_ in files:
                    Url = 'http://' + self.root.config.bindAddress + '/files/' + urllib.quote(file.Name())
                    fi = {
                          'Name':       file.Name(),
                          'Size':       file.Size(),
                          'Offset':     file.Offset(),
                          'Download':   file.Downloaded(),
                          'Progress':   file.Progress(),
                          'SavePath':   file.SavePath(),
                          'Url':        Url
                          }
                    retFiles.append(fi)
            output = json.dumps(retFiles)
            self.wfile.write(output)
        def peersHandler(self):
            self.send_response(200)
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
                       'Source':        peer.source,
                       'UpSpeed':       peer.up_speed/1024,
                       'DownSpeed':     peer.down_speed/1024,
                       'TotalDownload': peer.total_download,
                       'TotalUpload':   peer.total_upload,
                       'Country':       peer.country,
                       'Client':        peer.client
                       }
                ret.append(pi)
            output = json.dumps(ret)
            self.wfile.write(output)
        def trackersHandler(self):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            ret = list()
            for tracker in self.root.torrentHandler.get_torrent_info().trackers():
                pi = {
                        'Url':                tracker.url,
                        'NextAnnounceIn':        self.root.torrentHandler.status().next_announce.seconds,
                        'MinAnnounceIn':        10, # FIXME неясно, откуда брать
                        'ErrorCode':            0, #FIXME неясно, откуда брать
                        'ErrorMessage':        u'', #FIXME неясно, откуда брать
                        'Message':            u'', #FIXME неясно, откуда брать
                        'Tier':                tracker.tier,
                        'FailLimit':            tracker.fail_limit,
                        'Fails':                tracker.fails,
                        'Source':                tracker.source,
                        'Verified':            tracker.verified,
                        'Updating':            tracker.updating,
                        'StartSent':            tracker.start_sent,
                        'CompleteSent':        tracker.complete_sent,
                        }
                ret.append(pi)
            output = json.dumps(ret)
            self.wfile.write(output)

class Pyrrent2http(object):
    def __init__(self):
        self.torrentHandle = None
        self.forceShutdown = False
        self.session = None
        self.magnet = false
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
        if self.config.uri.startswith('magnet:'):
            self.magnet = True
        if self.config.resumeFile != '' and not self.config.keepFiles:
            logging.error('Usage of option --resume-file is allowed only along with --keep-files')
            sys.exit(1)
    
    def buildTorrentParams(self, uri):
        fileUri = urlparse.urlparse(uri)
        torrentParams = {}
        if self.magnet:
            torrentParams['url'] =  uri
        elif fileUri.scheme == 'file':
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
            torrentParams['ti'] = torrent_info
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
        if self.config.noSparseFile or self.magnet:
            logging.info('Disabling sparse file support...')
            torrentParams["storage_mode"] = lt.storage_mode_t.storage_mode_allocate
        return torrentParams
    
    def addTorrent(self):
        self.torrentParams = self.buildTorrentParams(self.config.uri)
        logging.info('Adding torrent')
        self.torrentHandle = self.session.add_torrent(self.torrentParams)
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
        logging.info('Downloading torrent: %s', self.torrentHandle.get_torrent_info().name())
        self.TorrentFS = TorrentFS(self, self.torrentHandle, self.config.fileIndex)
    
    def startHTTP(self):
        logging.info('Starting HTTP Server...')
        handler = HttpHandlerFactory(self)
        # if config.idleTimeout > 0 {
        #     connTrackChannel := make(chan int, 10)
        #     handler = NewConnectionCounterHandler(connTrackChannel, mux)
        #     go inactiveAutoShutdown(connTrackChannel)
        # }
        logging.info('Listening HTTP on %s...\n', self.config.bindAddress)
        self.httpListener = ThreadingHTTPServer(tuple(self.config.bindAddress.split(':')), handler)
        thread = threading.Thread(target = self.httpListener.serve_forever)
        thread.start()
    
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
        
        rand = SystemRandom(time.time())
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
        
    def stats(self):
        status = self.torrentHandle.status()
        dhtStatusStr = ''
        if not status.has_metadata:
            return
        if self.config.showAllStats or self.config.showOverallProgress:
            sessionStatus = self.session.status()
            if self.session.is_dht_running():
                dhtStatusStr = ', DHT nodes: %d' % (sessionStatus.dht_nodes,)
            errorStr = ''
            if len(status.error) > 0:
                errorStr = ' (%s)' % (status.error,)
            logging.info('%s, overall progress: %.2f%%, dl/ul: %.3f/%.3f kbps, peers/seeds: %d/%d' + dhtStatusStr + errorStr % (
                          str(status.state),
                          status.progres * 100,
                          float(status.download_rate)/1024,
                          float(status.upload_rate)/1024,
                          status.num_peers,
                          status.num_seeds
                          )
                         )
            if self.config.showFilesProgress or self.config.showAllStats:
                str_ = 'Files: '
                for i, f in enumerate(self.TorrentFS.Files()):
                    str_ += '[%d] %.2f%% ' % (i, f.Progress()*100)
                logging.info(str_)
            if (self.config.showPiecesProgress or self.config.showAllStats) and self.torrentFS.LastOpenedFile() != None:
                self.torrentFS.LastOpenedFile().ShowPieces()
    def consumeAlerts(self):
        alerts = self.session.pop_alerts()
        for alert in alerts:
            if isinstance(alert, lt.save_resume_data_alert):
                self.processSaveResumeDataAlert(alert)
    def waitForAlert(self, alertClass, timeout):
        start = time.time()
        while True:
            alert = self.session.wait_for_alert(100)
            if (time.time() - start) > timeout:
                return None
            if alert is not None:
                alert = self.session.pop_alert()
                if isinstance(alert, alertClass):
                    return alert
    def loop(self):
        def sigterm_handler(_signo, _stack_frame):
            self.forceShutdown = True
        signal.signal(signal.SIGTERM, sigterm_handler)
        statsTicker = RepeatedTimer(30)
        saveResumeDataTicker = RepeatedTimer(5)
        time_start = time.time()
        while True:
            if self.forceShutdown:
                self.httpListener.shutdown()
                return
            if time.time() - time_start > 0.5:
                self.consumeAlerts()
                self.torrentFS.LoadFileProgress()
                state = self.torrentHandle.status().state
                if self.config.exitOnFinish and (state == state.finished or state == state.seeding):
                    self.forceShutdown = True
                if os.getppid() == 1:
                    self.forceShutdown = True
                time_start = time.time()
            if statsTicker.true:
                self.stats()
            if saveResumeDataTicker.true:
                self.saveResumeData(True)
    def processSaveResumeDataAlert(self, alert):
        logging.info('Saving resume data to: %s', config.resumeFile)
        data = lt.bencode(alert.resume_data)
        try:
            with open(self.config.resumeFile, 'wb') as f:
                f.write(data)
        except IOError as e:
            _, strerror = e.args
            logging.error(strerror)
    def saveResumeData(self, async = False):
        if not self.torrentHandle.status().need_save_resume or self.config.resumeFile == '':
            return False
        self.torrentHandle.save_resume_data(3)
        if not async:
            alert = self.waitForAlert(lt.save_resume_data_alert, 5)
            if alert == None:
                return False
            self.processSaveResumeDataAlert(alert)
        return True
    def saveSessionState(self):
        if self.config.stateFile == '':
            return
        entry = self.session.save_state()
        data = lt.bencode(entry)
        logging.info('Saving session state to: %s', self.config.stateFile)
        try:
            with open(self.config.stateFile, 'wb') as f:
                f.write(data)
        except IOError as e:
            _, strerror = e.args
            logging.error(strerror)
    def removeFiles(self, files):
        for file in files:
            try:
                os.remove(file)
            except Exception as e:
                _, strerror = e.args
                logging.error(strerror)
            else:
                path = os.path.dirname(file)
                savePath = os.path.abspath(self.config.downloadPath)
                savePath = savePath[-1] == os.path.sep and savePath[:-1] or savePath
                while path != savePath:
                    os.remove(path)
                    path_ = os.path.dirname(path)
                    path = path_[-1] == os.path.sep and path_[:-1] or path_
    def filesToRemove(self):
        files = []
        if self.torrentFS.HasTorrentInfo():
            for file in self.torrentFS.Files():
                if (not self.config.keepComplete or not file.IsComplete()) and (not self.config.keepIncomplete or file.IsComplete()):
                    if os.path.exists(file.SavePath()):
                        files.append(file.SavePath())
    def removeTorrent(self):
        files = []
        state = self.torrentHandle.status().state
        if state != state.checking_files and state != state.queued_for_checking and not self.config.keepFiles:
            if not self.config.keepComplete and not self.config.keepIncomplete:
                delete_files = True
            else:
                delete_files = False
                files = self.filesToRemove()
        logging.info('Removing the torrent')
        self.session.remove_torrent(self.torrentHandle, delete_files = delete_files)
        if delete_files or len(files) > 0:
            logging.info('Waiting for files to be removed')
            self.waitForAlert(lt.torrent_deleted_alert, 15)
            self.removeFiles(files)
    def shutdown(self):
        logging.info('Stopping pyrrent2http...')
        self.torrentFS.Shutdown()
        if self.session != None:
            self.session.pause()
            self.waitForAlert(lt.torrent_paused_alert, 10)
            if self.torrentHandle is not None:
                self.saveResumeData(False)
                self.saveSessionState()
                self.removeTorrent()
            logging.info('Aborting the session')
            del self.session
        logging.info('Bye bye')
        sys.exit(0)

if __name__ == '__main__':
    pyrrent2http = Pyrrent2http()
    pyrrent2http.parseFlags()

    pyrrent2http.startSession()
    pyrrent2http.startServices()
    pyrrent2http.addTorrent()

    pyrrent2http.startHTTP()
    pyrrent2http.loop()
    pyrrent2http.shutdown()