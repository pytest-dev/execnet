from __future__ import annotations

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
tasks: list[int] | None = list(range(10))  # a list of tasks, here just integers
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
        print(f"other side {channel.gateway.id} returned {item!r}")
    if not tasks and tasks is not None:
        print("no tasks remain, sending termination request to all")
        mch.send_each(None)
        tasks = None
    if tasks:
        task = tasks.pop()
        channel.send(task)
        print(f"sent task {task!r} to {channel.gateway.id}")

group.terminate()
