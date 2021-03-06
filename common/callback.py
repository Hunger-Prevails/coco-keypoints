import time
import logging


class Speedometer(object):
    def __init__(self, batch_size, frequency):
        self.batch_size = batch_size
        self.frequency = frequency
        self.init = False
        self.tic = 0
        self.last_count = 0

    def __call__(self, param):
        """Callback to Show speed."""
        count = param.nbatch
        if self.last_count > count:
            self.init = False
        self.last_count = count

        if self.init:
            if count % self.frequency == 0:
                speed = self.frequency * self.batch_size / (time.time() - self.tic)
                if param.eval_metric is not None:
                    name, value = param.eval_metric.get()
                    param.eval_metric.reset()
                    s = "Epoch[%d] Batch [%d]  Speed: %.2f samples/sec," % (param.epoch, count, speed)
                    for n, v in zip(name, value):
                        s += "  %s=%.6f," % (n, v)
                    logging.info(s)
                else:
                    logging.info("Epoch[%d] Batch [%d]  Speed: %.2f samples/sec", param.epoch, count, speed)
                self.tic = time.time()
        else:
            self.init = True
            self.tic = time.time()
