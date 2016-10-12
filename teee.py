import eventlet
import time,os,sys
import multiprocessing,threading
def fun():
    while True:
        time.sleep(1)
        print(os.getpid())
#
def a():
    print(11)
if __name__=="__main__":
    #p1=multiprocessing.Process(target=fun)
    t1=threading.Thread(target=fun)

 #   t1.daemon=True
    print(111)
    t1.start()
#    t1.join()

    #p1.daemon=True
    #p1.start()
    print(1)
    quit()
 #    time.sleep(2)