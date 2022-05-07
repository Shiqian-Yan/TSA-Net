import os
import argparse
parser = argparse.ArgumentParser(description='label Generator')
parser.add_argument('--saveDir', default='/home/stu4/user/ysq/DIY_2007/DIY_dataset/VOC2007/ImageSets/Main',
                    type=str, help='choose the saveDir')
parser.add_argument('--dataDir', default='/home/stu4/user/ysq/DIY_2007/DIY_dataset/VOC2007/JPEGImages',
                    type=str, help='choose the dataDir')
parser.add_argument('--name', default='train',
                    type=str, help='select train, val or test')
args = parser.parse_args()

def genTxt(saveDir,dataDir, name='train'):
    dirname = saveDir
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    subdirs = os.listdir(dataDir)
    #subdirs.sort(key= lambda x:int(x[:-4]))
    filename = os.path.join(dirname, name+'.txt')
    print(filename)
    if os.path.exists(filename):
        os.remove(filename)
    for dir in subdirs:
        dirlist = dir.split('_')
        dirdir=dirlist[-1]
        if name+'.jpg'==dirdir :
            print(dir)
            string = ''   
            string = dir[:-4]+'\n'
            with open(filename, mode='a+') as f:
                f.write(string)
                f.close()

if __name__ == "__main__":
    genTxt(saveDir=args.saveDir,dataDir=args.dataDir, name=args.name)
