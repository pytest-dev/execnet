# -*- coding: utf-8 -*-
import execnet

group = execnet.Group()
for i in range(4):  # 4 CPUs
    group.makegateway()


def process_item(channel):
    # task processor, sits on each CPU
    import time
    import random

    channel.send("ready")
    for x in channel:
        if x is None:  # we can shutdown
            break
        # sleep random time, send result
        time.sleep(random.randrange(3))
        channel.send(x * 10)


# execute taskprocessor everywhere
mch = group.remote_exec(process_item)

# get a queue that gives us results
q = mch.make_receive_queue(endmarker=-1)
tasks = range(10)  # a list of tasks, here just integers
terminated = 0
while 1:
    channel, item = q.get()
    if item == -1:
        terminated += 1
        print("terminated %s" % channel.gateway.id)
        if terminated == len(mch):
            print("got all results, terminating")
            break
        continue
    if item != "ready":
        print("other side {} returned {!r}".format(channel.gateway.id, item))
    if not tasks:
        print("no tasks remain, sending termination request to all")
        mch.send_each(None)
        tasks = -1
    if tasks and tasks != -1:
        task = tasks.pop()
        channel.send(task)
        print("sent task {!r} to {}".format(task, channel.gateway.id))

group.terminate()
