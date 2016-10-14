# g_list=[0]
# class MyException(Exception):
#     def __init__(self,sum,name):
#         Exception.__init__(self)
#         self.sum=sum
#         self.name=name
#     def __str__(self):
#         return repr("MyException:%s--%s" % (str(self.sum),self.name))
# def get_list():
#     raise  MyException,("ERROROROROR","ssss")
#     return 0
# a="abc" \
#   "cdb" \
#   "abc"
# print(a)
import MySQLdb

conn = MySQLdb.connect(host='172.16.0.180', user='root', passwd='123456', db='devicewatchservicedb')
print "connection is ok!"
conn.autocommit(False)
#conn.autocommit(True)

cursor = conn.cursor()

# MySQLdb execute can craete procedure
sql = "CREATE PROCEDURE demo_proc5(IN protocol VARCHAR(10) ,IN deviceID VARCHAR(100) ,IN channel VARCHAR(2) ,IN streamType VARCHAR(1) ,IN url VARCHAR(50)) \
BEGIN \
    DECLARE n int DEFAULT 0;\
    SELECT COUNT(*) INTO streamurl FROM streamurl WHERE protocol=protocol AND deviceID=deviceID AND channel=channel AND streamType=streamType;\
    if n=0 THEN \
    INSERT INTO streamurl(protocol,deviceID,channel,streamType,url,createDate,updateDate) VALUES (protocol,deviceID,channel,streamType,url,now(),now());\
    ELSE \
    UPDATE streamurl set url=url,updateDate=updateDate WHERE protocol=protocol AND deviceID=deviceID AND channel=channel AND streamType=streamType; \
    END if; \
    END;"
cursor.execute(sql)
print "procedure created suceefully!!"


def func(procname,seq):
    sql = "select "
    paralist = list(seq)
    j = 0
    for i in paralist:
        paralist[j] = "@_"+procname+"_"+str(i)
        j=j+1
    delimiter = ','
    paralist = delimiter.join(paralist)
    sql = sql + paralist
    return sql
seq = (0,1)
name = 'demo_proc'
sql = func(name,seq)
print sql
#http://www.cnblogs.com/luoshulin/archive/2009/10/28/1591385.html
aaa=1
bbb=1
try:
    seq = (aaa,bbb)
    cursor.callproc('demo_proc4',seq)
    #cursor.callproc('demo_proc',(aaa,bbb))
    cursor.execute(sql)
    conn.commit()
    #cursor.execute("select @_demo_proc_0,@_demo_proc_1")#Note: the parameter from 0
    row = cursor.fetchone()
    set_len = len(row)
    while row:
        for i in range(0,set_len):
            print row[i]
        row = cursor.fetchone()
except MySQLdb.Error,e:
    print e.args[0],e.args[1]