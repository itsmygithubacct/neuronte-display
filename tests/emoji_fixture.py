"""Analytic colour half fixture, independent of trained emoji artwork.

The field spans 1.25-x over two cells; its code-conditioned palette paints the
left half red and the right quarter green. Light theme halves the intensities.
Derived from the Neuronte MIT half-API acceptance fixture.
"""
import struct
import numpy as np


def fixture():
    h=r=8
    header=struct.pack('<8s5If6I43I',b'NEURONTE',3,6,64,h,3,1.0,1,32,5,r,2,32,*([1]*43))
    dark=np.zeros((32,3),np.uint8); light=dark.copy()
    dark[2],dark[3]=(255,0,0),(0,255,0)
    light[2],light[3]=(128,0,0),(0,128,0)
    header+=dark.tobytes()+light.tobytes()
    first=np.zeros((h,90),np.float32); bias=np.zeros(h,np.float32)
    first[0,0],first[0,46],bias[0]=-.5,-.5,2.25
    hidden=np.zeros((h,h),np.float32); hidden[0,0]=1
    zero=np.zeros(h,np.float32)
    output=np.zeros((1,h),np.float32); output[0,0]=1
    head=np.zeros((r,64),np.float32); head[0,20]=1
    headbias=np.zeros(r,np.float32); headbias[0]=1
    palette=np.zeros((5,r),np.float32); palette[0,0]=1
    arrays=(first,bias,hidden,zero,hidden,zero,output,np.array([-2]),head,headbias,palette,np.array([-1,1,-1,-1,-1]))
    data=header+b''.join(array.astype('<f4').tobytes() for array in arrays)
    return data+struct.pack('<IHHIHHfI',0x1f600,0,0,0x12f600,1,0,.25,0)
