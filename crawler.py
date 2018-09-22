import argparse
import http.client
import re
import socket
import sqlite3
import sys
import time
from enum import Enum

import tweepy
import urllib3
import yam
from tweepy import OAuthHandler, Stream
from tweepy.streaming import StreamListener
from datetime import date
from pathlib import Path


class TaskState(Enum):
    WAITING_STATUS1 = 1
    WAITING_STATUS2 = 2
    DONE = 3


# statuses_look API accepts up to 100 status ids
MAX_NUM_SIDS = 100


# A class represents following conversation using in_reply_to_status_id
class FollowConversationTask:
    def __init__(self, status3):
        self.status3 = status3
        self.status2 = None
        self.status1 = None
        self.state = TaskState.WAITING_STATUS2


class QueueListener(StreamListener):
    def __init__(self, db_path, lang="ja"):
        super().__init__()
        cfg = yam.get_keys()
        self.auth = OAuthHandler(cfg['consumer_key'], cfg['consumer_secret'])
        self.auth.set_access_token(cfg['access_token'],
                                   cfg['access_token_secret'])
        self.api = tweepy.API(self.auth)
        self.db = db_path
        self.lang = lang
        self.sids_to_lookup = []
        self.tasks = {}

    def is_target_lang_tweet(self, status):
        return status.user.lang == self.lang

    @staticmethod
    def has_in_reply_to(status):
        return isinstance(status.in_reply_to_status_id, int)

    def add_task(self, status3):
        self.sids_to_lookup.append(status3.in_reply_to_status_id)
        print(".", end='', flush=True)
        self.tasks[status3.in_reply_to_status_id] = FollowConversationTask(
            status3)
        if len(self.sids_to_lookup) >= MAX_NUM_SIDS:
            print("\nCalling statuses_lookup API...")
            statues = self.api.statuses_lookup(self.sids_to_lookup)
            self.sids_to_lookup = []
            for status in statues:
                if status.id in self.tasks:
                    task = self.tasks[status.id]
                    del self.tasks[status.id]
                    self.handle_task(task, status)
                else:
                    print("waring not found")

    def handle_task(self, task, status):
        if task.state == TaskState.WAITING_STATUS2:
            task.status2 = status
            if task.status3.user.id == task.status2.user.id or not self.has_in_reply_to(
                    status):
                return
            else:
                task.state = TaskState.WAITING_STATUS1
                self.tasks[status.in_reply_to_status_id] = task
                print(".", end='', flush=True)
                self.sids_to_lookup.append(
                    status.in_reply_to_status_id)
        elif task.state == TaskState.WAITING_STATUS1:
            task.status1 = status
            if task.status1.user.id != task.status3.user.id or self.has_in_reply_to(
                    status):
                return
            else:
                task.state = TaskState.DONE
                self.print_conversation(task.status1, task.status2,
                                        task.status3)
                self.insert_conversation(task.status1, task.status2,
                                         task.status3)

    def on_status(self, status3):
        if self.is_target_lang_tweet(status3) and self.has_in_reply_to(status3):
            self.add_task(status3)

    def print_conversation(self, status1, status2, status3):
        print("================================================================"
              "\n{}:{}\n{}:{}\n{};{}".format(status1.id,
                                             self.sanitize_text(status1.text),
                                             status2.id,
                                             self.sanitize_text(status2.text),
                                             status3.id,
                                             self.sanitize_text(status3.text)))

    def insert_conversation(self, status1, status2, status3):
        for status in [status1, status2, status3]:
            self.insert_status(status)

        conn = sqlite3.connect(self.db)
        c = conn.cursor()
        try:
            c.execute(
                "insert into conversation (sid1, sid2, sid3) values (?, ?, ?)",
                [
                    status1.id,
                    status2.id,
                    status3.id
                ])
        except sqlite3.IntegrityError as e:
            print(e)
        finally:
            conn.commit()
            conn.close()

    @staticmethod
    def sanitize_text(text):
        return re.sub("\s+", ' ', text).strip()

    def insert_status(self, status):
        conn = sqlite3.connect(self.db)
        c = conn.cursor()
        text = self.sanitize_text(status.text)
        try:
            c.execute(
                "insert into status "
                "(id, text, in_reply_to_status_id, user_id, "
                "created_at, is_quote_status) "
                "values (?, ?, ?, ?, ?, ?)",
                [
                    status.id,
                    text,
                    status.in_reply_to_status_id,
                    status.user.id,
                    status.created_at.timestamp(),
                    status.is_quote_status
                ])
        except sqlite3.IntegrityError as e:
            # There can be same status already
            print(e)
        finally:
            conn.commit()
            conn.close()

    def on_error(self, status):
        print('ON ERROR:', status)

    def on_limit(self, track):
        print('ON LIMIT:', track)


def make_db(db_path):
    sql_status = """
        create table status(
        id integer NOT NULL,
        text text NOT NULL,
        in_reply_to_status_id integer default 0,
        user_id integer NOT NULL,
        is_quote_status integer NOT NULL,
        created_at integer NOT NULL,
        CONSTRAINT status_id PRIMARY KEY (id)
        );
        """
    sql_conversation = """
        create table conversation(
        sid1 integer NOT NULL,
        sid2 integer NOT NULL,
        sid3 integer NOT NULL,
        CONSTRAINT converstaion_id PRIMARY KEY (sid1, sid2, sid3)
        );
        """

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(sql_status)
    cur.execute(sql_conversation)
    conn.commit()
    conn.close()

def main():
    # {path}は自分の環境にあわせる
    db_path = str(Path.home()) + "{path}/Conversation." + date.today().strftime("20%y.%m.%d") + ".db"
    parser = argparse.ArgumentParser()
    parser.add_argument('--new', type=int, default=-1,
                        help='-1 indicates new database...')
    args = parser.parse_args()
    if args.new == -1:
        make_db(db_path)
    listener = QueueListener(db_path)
    stream = Stream(listener.auth, listener)
    print("Listening...\n")
    delay = 0.25
    try:
        while True:
            try:
                stream.sample()
            except KeyboardInterrupt:
                print('Stopped')
                return
            except urllib3.exceptions.ProtocolError as e:
                print("Incomplete read", e)
            except urllib3.exceptions.ReadTimeoutError as e:
                print("Read Timeout", e)
            except (socket.error, http.client.HTTPException):
                print("HTTP error waiting for a few seconds")
                time.sleep(delay)
                delay += 0.25
    finally:
        stream.disconnect()


if __name__ == '__main__':
    sys.exit(main())
