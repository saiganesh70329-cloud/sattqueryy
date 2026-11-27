import os, io, json, base64, uuid, secrets, logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
import numpy as np
from PIL import Image
from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey, LargeBinary, UniqueConstraint, text, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

ROOT=Path(__file__).resolve().parent.parent
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
def normalize_database_url(url):
 if url.startswith('postgres://'):
  return 'postgresql+psycopg://' + url[len('postgres://'):]
 if url.startswith('postgresql://'):
  return 'postgresql+psycopg://' + url[len('postgresql://'):]
 return url
IS_VERCEL=os.getenv('VERCEL')=='1'
DATABASE_URL=normalize_database_url(os.getenv('DATABASE_URL','sqlite://' if IS_VERCEL else 'sqlite:///./satquery.db'))
APP_ENV=os.getenv('APP_ENV','production' if IS_VERCEL else 'development').lower()
DATABASE_CONFIGURATION_ERROR=IS_VERCEL and not DATABASE_URL.startswith('postgresql+psycopg://')
AUTH_CONFIGURATION_ERROR=IS_VERCEL and not os.getenv('SECRET_KEY')
OBJECT_STORAGE_CONFIG={
 'endpoint_url':os.getenv('OBJECT_STORAGE_ENDPOINT_URL',''),
 'bucket':os.getenv('OBJECT_STORAGE_BUCKET',''),
 'region':os.getenv('OBJECT_STORAGE_REGION',''),
 'access_key_id':os.getenv('OBJECT_STORAGE_ACCESS_KEY_ID',''),
 'secret_access_key':os.getenv('OBJECT_STORAGE_SECRET_ACCESS_KEY',''),
}
OBJECT_STORAGE_ENABLED=bool(os.getenv('BLOB_STORE_ID')) or all(OBJECT_STORAGE_CONFIG.values())
if APP_ENV=='production':
 required=['DATABASE_URL','SECRET_KEY','FRONTEND_ORIGINS','BLOB_STORE_ID','BLOB_UPLOAD_URL']
 if os.getenv('ADMIN_BOOTSTRAP_ENABLED','false').lower()=='true': required.extend(('ADMIN_EMAIL','ADMIN_PASSWORD'))
 if not DATABASE_URL.startswith('postgresql+psycopg://'): required.append('DATABASE_URL')
 missing=[key for key in required if not os.getenv(key)]
 if missing:
  if IS_VERCEL:
   logging.getLogger(__name__).error('Missing required deployment configuration: %s', ', '.join(sorted(set(missing))))
  else:
   raise RuntimeError('Missing required production environment variables: '+', '.join(sorted(set(missing))))
engine=create_engine(DATABASE_URL,connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite') else {},pool_pre_ping=True,hide_parameters=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False)
Base=declarative_base(); pwd=CryptContext(schemes=['bcrypt'],deprecated='auto')
SECRET=os.getenv('SECRET_KEY') or secrets.token_urlsafe(32); ALG='HS256'
class User(Base):
 __tablename__='users'; id=Column(Integer,primary_key=True); email=Column(String,unique=True,index=True); name=Column(String); password=Column(String); role=Column(String,default='user'); active=Column(Boolean,default=True); created=Column(DateTime,default=datetime.utcnow)
class Analysis(Base):
 __tablename__='analyses'; id=Column(String,primary_key=True); user_id=Column(Integer,ForeignKey('users.id')); kind=Column(String); name=Column(String); location=Column(String,default=''); lat=Column(String,default=''); lon=Column(String,default=''); source=Column(String,default='USER UPLOAD'); result=Column(Text); created=Column(DateTime,default=datetime.utcnow)
class Notification(Base):
 __tablename__='notifications'; id=Column(Integer,primary_key=True); user_id=Column(Integer,ForeignKey('users.id')); message=Column(String); created=Column(DateTime,default=datetime.utcnow)
class OriginalImage(Base):
 __tablename__='original_images'; id=Column(Integer,primary_key=True); analysis_id=Column(String,ForeignKey('analyses.id',ondelete='CASCADE'),index=True); role=Column(String); filename=Column(String); content_type=Column(String); data=Column(LargeBinary); created=Column(DateTime,default=datetime.utcnow); __table_args__=(UniqueConstraint('analysis_id','role',name='uq_original_image_analysis_role'),)
OBJECT_REF_PREFIX=b'SATQUERY_OBJECT://'
_object_store_client=None
MAX_UPLOAD_BYTES=15*1024*1024
ALLOWED_IMAGE_TYPES={'image/png','image/jpeg','image/tiff','image/webp'}
def object_store():
 global _object_store_client
 if not OBJECT_STORAGE_ENABLED: raise RuntimeError('Persistent object storage is not configured')
 if _object_store_client is None:
  if os.getenv('BLOB_STORE_ID'):
   from vercel.blob.sync import get_blob, put_blob
   class VercelPrivateBlobStore:
    def put_object(self,Bucket,Key,Body,ContentType):
     return put_blob(Key,bytes(Body),access='private',content_type=ContentType)
    def get_object(self,Bucket,Key):
     result=get_blob(Key,access='private')
     return {'Body':io.BytesIO(result.body),'ContentLength':len(result.body),'ContentType':result.metadata.content_type}
   _object_store_client=VercelPrivateBlobStore()
   return _object_store_client
  import boto3
  from botocore.config import Config
  _object_store_client=boto3.client('s3',endpoint_url=OBJECT_STORAGE_CONFIG['endpoint_url'],region_name=OBJECT_STORAGE_CONFIG['region'],aws_access_key_id=OBJECT_STORAGE_CONFIG['access_key_id'],aws_secret_access_key=OBJECT_STORAGE_CONFIG['secret_access_key'],config=Config(signature_version='s3v4',s3={'addressing_style':'path'}))
 return _object_store_client
def safe_upload_key(key,user_id):
 import re
 return bool(re.fullmatch(rf'uploads/{int(user_id)}/[0-9a-f]{{32}}\.(?:png|jpg|jpeg|tif|tiff|webp)',key or ''))
def stream_object(body):
 try:
  while True:
   chunk=body.read(64*1024)
   if not chunk: break
   yield chunk
 finally:
  body.close()
def signed_preview_url(analysis_id,user_id,field):
 claim={'sub':str(user_id),'analysis_id':analysis_id,'field':field,'exp':datetime.utcnow()+timedelta(minutes=15)}
 signed=jwt.encode(claim,SECRET,algorithm=ALG)
 return f'/api/analysis/{analysis_id}/previews/{field}?token={signed}'
def public_result(analysis_id,user_id,result):
 public=json.loads(json.dumps(result))
 if APP_ENV=='production':
  for field,value in public.items():
   if field.endswith('image') and isinstance(value,str) and (value.startswith('data:image/') or value.startswith(OBJECT_REF_PREFIX.decode())):
    public[field]=signed_preview_url(analysis_id,user_id,field)
 return public
def store_result_previews(result,analysis_id,user_id):
 if not OBJECT_STORAGE_ENABLED: return
 client=object_store()
 for field,value in list(result.items()):
  if field.endswith('image') and isinstance(value,str) and value.startswith('data:image/'):
   try:
    header,payload=value.split(',',1); content_type=header[5:].split(';',1)[0]; raw=base64.b64decode(payload,validate=True)
   except (ValueError,TypeError): raise RuntimeError('Invalid generated preview image')
   if content_type not in ('image/png','image/jpeg','image/webp'): raise RuntimeError('Unsupported generated preview format')
   key=f'previews/{int(user_id)}/{analysis_id}/{field}'
   client.put_object(Bucket=OBJECT_STORAGE_CONFIG['bucket'],Key=key,Body=raw,ContentType=content_type)
   result[field]=OBJECT_REF_PREFIX.decode()+key
def _preview_bytes(analysis_id,user_id,field,value):
 if not isinstance(value,str): return None
 if value.startswith('data:image/'):
  try: return base64.b64decode(value.split(',',1)[1],validate=True)
  except (ValueError,IndexError): return None
 if value.startswith(OBJECT_REF_PREFIX.decode()):
  key=value[len(OBJECT_REF_PREFIX.decode()):]
  if not key.startswith(f'previews/{int(user_id)}/{analysis_id}/'): return None
  body=object_store().get_object(Bucket=OBJECT_STORAGE_CONFIG['bucket'],Key=key)['Body']
  try: return body.read()
  finally: body.close()
 return None
if os.getenv('SATQUERY_SKIP_SCHEMA_CREATE')!='1' and not DATABASE_CONFIGURATION_ERROR:
 Base.metadata.create_all(engine)
def db():
 if DATABASE_CONFIGURATION_ERROR: raise HTTPException(503,'Account storage is unavailable. Please try again later.')
 s=SessionLocal()
 try: yield s
 finally: s.close()
oauth=OAuth2PasswordBearer(tokenUrl='/api/login')
def current(token:str=Depends(oauth),s:Session=Depends(db)):
 try: data=jwt.decode(token,SECRET,algorithms=[ALG]); u=s.query(User).filter_by(id=int(data['sub'])).first()
 except Exception: u=None
 if not u or not u.active: raise HTTPException(401,'Invalid or inactive account')
 return u
def admin(u=Depends(current)):
 if u.role!='admin': raise HTTPException(403,'Admin access required')
 return u
app=FastAPI(title='SATQUERY API',version='1.0')
configured_origins=[origin.strip().rstrip('/') for origin in os.getenv('FRONTEND_ORIGINS','').split(',') if origin.strip()]
if not configured_origins and IS_VERCEL and os.getenv('VERCEL_URL'):
 configured_origins=['https://'+os.environ['VERCEL_URL'].strip().rstrip('/')]
if not configured_origins and APP_ENV!='production': configured_origins=['http://localhost:5173','http://127.0.0.1:5173','http://localhost:5174','http://127.0.0.1:5174']
if APP_ENV=='production' and any(origin=='*' or urlsplit(origin).scheme!='https' or not urlsplit(origin).netloc or urlsplit(origin).path or urlsplit(origin).query or urlsplit(origin).fragment for origin in configured_origins):
 raise RuntimeError('Production FRONTEND_ORIGINS must contain exact HTTPS origins without paths or wildcards')
app.add_middleware(CORSMiddleware,allow_origins=configured_origins,allow_methods=['GET','POST','PATCH'],allow_headers=['Authorization','Content-Type'],expose_headers=['Content-Disposition'])
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request:Request,exc:StarletteHTTPException):
 return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail},headers=exc.headers)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request:Request,exc:RequestValidationError):
 errors=[{'loc':error.get('loc',[]),'msg':error.get('msg','Invalid value'),'type':error.get('type','value_error')} for error in exc.errors()]
 return JSONResponse(status_code=422,content={'detail':errors})
@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request:Request,exc:SQLAlchemyError):
 logging.getLogger(__name__).exception('Database operation failed for %s %s',request.method,request.url.path)
 return JSONResponse(status_code=503,content={'detail':'Account storage is temporarily unavailable. Please try again later.'})
@app.exception_handler(Exception)
async def unhandled_exception_handler(request:Request,exc:Exception):
 logging.getLogger(__name__).exception('Unhandled API exception for %s %s',request.method,request.url.path)
 return JSONResponse(status_code=500,content={'detail':'The request could not be completed. Please try again later.'})
@app.on_event('startup')
def startup():
 if APP_ENV=='production' and os.getenv('ADMIN_BOOTSTRAP_ENABLED','false').lower()!='true': return
 s=SessionLocal(); email=os.getenv('ADMIN_EMAIL','admin@satquery.local')
 admin_password=os.getenv('ADMIN_PASSWORD')
 if admin_password and not s.query(User).filter_by(email=email).first(): s.add(User(email=email,name='SATQUERY Admin',password=pwd.hash(admin_password),role='admin')); s.commit()
 s.close()
class Credentials(BaseModel): email:str; password:str
class Register(Credentials): name:str
class UploadRequest(BaseModel): filename:str; content_type:str; size:int
@app.get('/api/health')
def health():
 if DATABASE_CONFIGURATION_ERROR: raise HTTPException(503,'Account storage is unavailable. Please try again later.')
 try:
  with engine.connect() as connection: connection.execute(text('SELECT 1'))
 except SQLAlchemyError:
  raise HTTPException(503,'Database unavailable')
 return {'status':'ok','database':'postgresql' if DATABASE_URL.startswith('postgresql') else 'sqlite','data_provider':'upload/sample only'}
@app.get('/api/uploads/config')
def upload_config(): return {'direct_uploads':OBJECT_STORAGE_ENABLED}
@app.post('/api/uploads/presign')
def presign_upload(v:UploadRequest,u=Depends(current)):
 if not OBJECT_STORAGE_ENABLED: raise HTTPException(503,'Direct uploads are not configured')
 if v.content_type not in ALLOWED_IMAGE_TYPES: raise HTTPException(415,'Upload PNG, JPEG, TIFF, or WebP imagery')
 if v.size<1 or v.size>MAX_UPLOAD_BYTES: raise HTTPException(413,'Maximum upload size is 15 MB')
 if os.getenv('BLOB_STORE_ID'):
  import urllib.request
  extension={'image/png':'png','image/jpeg':'jpg','image/tiff':'tiff','image/webp':'webp'}[v.content_type]
  key=f'uploads/{u.id}/{uuid.uuid4().hex}.{extension}'
  payload=json.dumps({'object_key':key,'content_type':v.content_type,'size':v.size}).encode()
  req=urllib.request.Request(os.environ['BLOB_UPLOAD_URL'].rstrip('/')+'/api/uploads/presign',data=payload,headers={'Content-Type':'application/json'},method='POST')
  try:
   with urllib.request.urlopen(req,timeout=10) as response: ticket=json.loads(response.read())
  except Exception: raise HTTPException(503,'Could not prepare secure image upload')
  return {'object_key':key,'upload_url':ticket['upload_url'],'method':'PUT','headers':{'Content-Type':v.content_type},'expires_in':300}
 key=f'uploads/{u.id}/{uuid.uuid4().hex}.png'
 try:
  url=object_store().generate_presigned_url('put_object',Params={'Bucket':OBJECT_STORAGE_CONFIG['bucket'],'Key':key,'ContentType':v.content_type},ExpiresIn=300)
 except Exception: raise HTTPException(503,'Could not prepare secure image upload')
 return {'object_key':key,'upload_url':url,'method':'PUT','headers':{'Content-Type':v.content_type},'expires_in':300}
def token(u): return jwt.encode({'sub':str(u.id),'exp':datetime.utcnow()+timedelta(hours=12)},SECRET,algorithm=ALG)
@app.post('/api/register')
def register(v:Register,s:Session=Depends(db)):
 if AUTH_CONFIGURATION_ERROR: raise HTTPException(503,'Account service is unavailable. Please try again later.')
 if len(v.password)<8: raise HTTPException(400,'Password must contain at least 8 characters')
 if len(v.password.encode('utf-8'))>72: raise HTTPException(400,'Password must be 72 bytes or fewer')
 email=v.email.strip().lower()
 if not email: raise HTTPException(400,'Email is required')
 if s.query(User).filter(func.lower(func.trim(User.email))==email).first(): raise HTTPException(409,'Email already registered')
 name=v.name.strip()
 if not name: raise HTTPException(400,'Name is required')
 u=User(email=email,name=name[:80],password=pwd.hash(v.password)); s.add(u)
 try: s.commit()
 except IntegrityError:
  s.rollback()
  if s.query(User).filter(func.lower(func.trim(User.email))==email).first(): raise HTTPException(409,'Email already registered')
  raise
 except SQLAlchemyError:
  s.rollback()
  raise
 s.refresh(u); return {'token':token(u),'user':{'id':u.id,'name':u.name,'email':u.email,'role':u.role}}
@app.post('/api/login')
def login(v:Credentials,s:Session=Depends(db)):
 if AUTH_CONFIGURATION_ERROR: raise HTTPException(503,'Account service is unavailable. Please try again later.')
 email=v.email.strip().lower()
 u=s.query(User).filter(func.lower(func.trim(User.email))==email).first()
 if not u or not u.active or not pwd.verify(v.password,u.password): raise HTTPException(401,'Email or password is incorrect')
 return {'token':token(u),'user':{'id':u.id,'name':u.name,'email':u.email,'role':u.role}}
@app.get('/api/me')
def me(u=Depends(current)): return {'id':u.id,'name':u.name,'email':u.email,'role':u.role}
def read_image(f:UploadFile):
 if f.content_type not in ALLOWED_IMAGE_TYPES: raise HTTPException(415,'Upload PNG, JPEG, TIFF, or WebP imagery')
 data=f.file.read(MAX_UPLOAD_BYTES+1)
 if len(data)>MAX_UPLOAD_BYTES: raise HTTPException(413,'Maximum upload size is 15 MB')
 try:
  im=Image.open(io.BytesIO(data)).convert('RGB'); im.thumbnail((720,720)); return np.asarray(im),data
 except Exception: raise HTTPException(400,'Could not decode image')
def resolve_image(upload,key,filename,user):
 key=key if isinstance(key,str) else None; filename=filename if isinstance(filename,str) else None
 if upload is not None and key: raise HTTPException(422,'Provide either an uploaded file or an object key')
 if upload is not None:
  image,raw=read_image(upload); return image,raw,Path(upload.filename or 'upload').name,upload.content_type,None
 if not key: raise HTTPException(422,'An image file is required')
 if not OBJECT_STORAGE_ENABLED or not safe_upload_key(key,user.id): raise HTTPException(403,'Invalid image upload reference')
 try:
  result=object_store().get_object(Bucket=OBJECT_STORAGE_CONFIG['bucket'],Key=key)
  body=result['Body']
  if result.get('ContentLength',0)>MAX_UPLOAD_BYTES:
   body.close(); raise HTTPException(413,'Maximum upload size is 15 MB')
  raw=body.read(MAX_UPLOAD_BYTES+1); body.close()
 except HTTPException: raise
 except Exception: raise HTTPException(400,'Uploaded image could not be retrieved')
 if len(raw)>MAX_UPLOAD_BYTES: raise HTTPException(413,'Maximum upload size is 15 MB')
 content_type=result.get('ContentType','application/octet-stream')
 upload_file=type('StoredUpload',(),{'content_type':content_type,'file':io.BytesIO(raw),'filename':Path(filename or 'upload').name})()
 image,_=read_image(upload_file)
 return image,raw,upload_file.filename,content_type,key
def png_data(arr):
 b=io.BytesIO(); Image.fromarray(arr.astype('uint8')).save(b,format='PNG'); return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
def flood_water_mask(im):
 import cv2
 rgb=np.asarray(im,dtype=np.uint8); r,g,b=[rgb[:,:,i].astype(np.float32) for i in range(3)]
 hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV).astype(np.float32); lab=cv2.cvtColor(rgb,cv2.COLOR_RGB2LAB).astype(np.float32)
 hue,sat,val=hsv[:,:,0],hsv[:,:,1],hsv[:,:,2]; light,lab_a,lab_b=lab[:,:,0],lab[:,:,1],lab[:,:,2]
 gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY).astype(np.float32); mean=cv2.GaussianBlur(gray,(7,7),0); mean2=cv2.GaussianBlur(gray*gray,(7,7),0)
 texture=np.sqrt(np.maximum(mean2-mean*mean,0)); smooth=texture<=18
 blue=(b>r*1.08)&(b>g*.95)&((b-r)>12)&(smooth|(sat>115))
 dark=(val<65)&(sat<110)&smooth
 light_limit=float(np.clip(np.percentile(light,70)+5,112,168))
 muddy=(hue>=5)&(hue<=32)&(sat>=24)&(sat<=175)&(lab_a>=130)&(lab_b>=132)&((r-g)>=13)&((g-b)>=5)&(light<=light_limit)&(texture<=10)
 mask=(blue|dark|muddy).astype(np.uint8); mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
 count,labels,stats,_=cv2.connectedComponentsWithStats(mask,8); minimum=max(16,int(mask.size*.0003)); keep=np.zeros(count,dtype=bool); keep[1:]=stats[1:,cv2.CC_STAT_AREA]>=minimum
 return keep[labels]
def flood_comparison(before,after):
 import cv2
 bh,bw=before.shape[:2]; ah,aw=after.shape[:2]; aspect_delta=abs((bw/bh)-(aw/ah))/max(bw/bh,aw/ah)
 warning=''; score=None; shift=(0.0,0.0); aligned=after
 if aspect_delta>.02:
  valid=False; warning='The image aspect ratios differ. Geographic/pixel alignment could not be established; newly water-covered percentage was not calculated.'
 else:
  if (ah,aw)!=(bh,bw): after=cv2.resize(after,(bw,bh),interpolation=cv2.INTER_AREA); aligned=after
  before_gray=cv2.cvtColor(before,cv2.COLOR_RGB2GRAY); after_gray=cv2.cvtColor(aligned,cv2.COLOR_RGB2GRAY)
  if min(float(before_gray.std()),float(after_gray.std()))<5:
   valid=False; warning='The images do not contain enough visual detail to check alignment; newly water-covered percentage was not calculated.'
  else:
   warp=np.eye(2,3,dtype=np.float32)
   try:
    stable=(np.linalg.norm(before.astype(np.float32)-aligned.astype(np.float32),axis=2)<45).astype(np.uint8)*255
    if float((stable>0).mean())<.15: raise cv2.error('Insufficient stable image area for alignment')
    score,warp=cv2.findTransformECC(before_gray,after_gray,warp,cv2.MOTION_TRANSLATION,(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,120,1e-5),stable)
    shift=(float(warp[0,2]),float(warp[1,2])); max_shift=max(2.0,min(bw,bh)*.03)
    valid=score>=.78 and abs(shift[0])<=max_shift and abs(shift[1])<=max_shift
   except cv2.error:
    valid=False
   if not valid:
    warning='A reliable visual alignment could not be established. Check that both images cover the same area at a similar scale and orientation; newly water-covered percentage was not calculated.'
   elif abs(shift[0])>.5 or abs(shift[1])>.5:
    aligned=cv2.warpAffine(aligned,warp,(bw,bh),flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,borderMode=cv2.BORDER_CONSTANT)
    warning='A small visual translation was corrected. Geographic alignment is not independently verified without georeferencing; interpret the comparison cautiously.'
   else:
    warning='Visual alignment check passed. Geographic alignment cannot be independently confirmed without georeferencing.'
 before_mask=flood_water_mask(before); after_mask=flood_water_mask(aligned)
 valid_pixels=np.ones((bh,bw),dtype=bool)
 if valid and (abs(shift[0])>.5 or abs(shift[1])>.5):
  valid_pixels=cv2.warpAffine(np.ones((bh,bw),dtype=np.uint8),warp,(bw,bh),flags=cv2.INTER_NEAREST|cv2.WARP_INVERSE_MAP,borderMode=cv2.BORDER_CONSTANT)>0
 if before_mask.shape==after_mask.shape:
  before_mask &= valid_pixels; after_mask &= valid_pixels
 new_mask=(after_mask&~before_mask) if valid else np.zeros_like(after_mask,dtype=bool)
 total=int(valid_pixels.sum()) if valid else None; before_pixels=int(before_mask.sum()); after_pixels=int(after_mask.sum()); new_pixels=int(new_mask.sum()) if valid else None
 color=np.array([255,35,180],dtype=np.float32); overlay=aligned.copy(); overlay[new_mask]=(.42*overlay[new_mask].astype(np.float32)+.58*color).astype(np.uint8)
 before_rgb=np.zeros_like(before); before_rgb[before_mask]=color.astype(np.uint8); after_rgb=np.zeros_like(aligned); after_rgb[after_mask]=color.astype(np.uint8); change_rgb=np.zeros_like(aligned); change_rgb[new_mask]=color.astype(np.uint8)
 return {'analysis_mode':'comparison','comparison_valid':bool(valid),'alignment_warning':warning,'alignment_score':round(float(score),3) if score is not None else None,'geographic_alignment_confirmed':False,'before_water_pixels':before_pixels,'after_water_pixels':after_pixels,'new_water_pixels':new_pixels,'compared_pixels':total,'before_water_percent':round(before_pixels/before_mask.size*100,2) if before_mask.size else None,'after_water_percent':round(after_pixels/after_mask.size*100,2) if after_mask.size else None,'new_water_percent':round(new_pixels/total*100,2) if valid and total else None,'area_estimate':None,'area_estimate_note':'No exact area estimate: verified georeferencing and ground pixel resolution are unavailable.','method':'RGB, HSV and CIELAB water-mask heuristic, followed by visual translation-alignment screening and after-minus-before mask comparison (baseline; not a trained model).','limitations':'Possible newly water-covered pixels are not confirmed flooding. Clouds, shadows, muddy soil, seasonal changes, image quality and residual misalignment can affect both masks. Permanent water detected in both dates is excluded from the new-water mask. Compare matched pre-event and post-event imagery and verify geographic alignment before interpreting.','before_image':png_data(before),'after_image':png_data(aligned),'before_mask_image':png_data(before_rgb),'after_mask_image':png_data(after_rgb),'change_mask_image':png_data(change_rgb),'image':png_data(overlay)}
def save(kind,name,location,lat,lon,result,u,s,original_images=()):
 aid=str(uuid.uuid4()); result['original_uploads_available']=bool(original_images); store_result_previews(result,aid,u.id); a=Analysis(id=aid,user_id=u.id,kind=kind,name=name[:100],location=location[:100],lat=lat[:24],lon=lon[:24],result=json.dumps(result)); s.add(a)
 for item in original_images:
  role,filename,content_type,data=item[:4]; storage_key=item[4] if len(item)>4 else None
  stored_data=OBJECT_REF_PREFIX+storage_key.encode('utf-8') if storage_key else data
  s.add(OriginalImage(analysis_id=aid,role=role,filename=filename,content_type=content_type,data=stored_data))
 s.add(Notification(user_id=u.id,message=f'{kind.title()} analysis completed: {name[:60]}')); s.commit(); return a
@app.post('/api/analyze/land')
def land(file:UploadFile|None=File(None),file_upload_key:str|None=Form(None),file_upload_filename:str|None=Form(None),name:str=Form('Land cover analysis'),location:str=Form(''),lat:str=Form(''),lon:str=Form(''),source:str=Form('USER UPLOAD'),u=Depends(current),s=Depends(db)):
 import cv2
 im,raw,filename,content_type,storage_key=resolve_image(file,file_upload_key,file_upload_filename,u); hsv=cv2.cvtColor(im,cv2.COLOR_RGB2HSV); r,g,b=[im[:,:,i].astype(float) for i in range(3)]; ndvi=(g-r)/(g+r+1); water=(b>r*1.10)&(b>g*1.02)&(hsv[:,:,1]<120); built=(hsv[:,:,1]<55)&(r>75)&(g>65)&~water; vegetation=(ndvi>.08)&~(water|built)
 agricultural=vegetation&(hsv[:,:,1]>95); forest=vegetation&(hsv[:,:,1]<45); other_vegetation=vegetation&~(agricultural|forest); bare=~(water|built|vegetation)
 masks={'Agricultural land':agricultural,'Forest':forest,'Water bodies':water,'Urban / built-up':built,'Bare land':bare,'Vegetation':other_vegetation}
 classes=[]; colors={'Water bodies':[40,120,230],'Vegetation':[65,180,90],'Urban / built-up':[230,90,70],'Bare land':[220,180,90],'Agricultural land':[170,205,65],'Forest':[25,110,65]}; out=np.zeros_like(im)
 for label,mask in masks.items():
  pct=round(float(mask.mean()*100),1); classes.append({'class':label,'percent':pct}); out[mask]=colors[label]
 source=source if source=='SAMPLE DATA' else 'USER UPLOAD'; result={'classes':classes,'method':'Optical RGB color and normalized green-red index heuristic','source':source,'limitations':'Not trained or calibrated. Class colors overlap and indicate a qualitative baseline; no accuracy or ground area claim.' ,'image':png_data(out)}
 a=save('land',name,location,lat,lon,result,u,s,[('original',filename,content_type or 'application/octet-stream',raw,storage_key)]); return {'id':a.id,'created':a.created.isoformat(),'result':public_result(a.id,u.id,result)}
@app.post('/api/analyze/change')
def change(earlier:UploadFile|None=File(None),later:UploadFile|None=File(None),earlier_upload_key:str|None=Form(None),later_upload_key:str|None=Form(None),earlier_upload_filename:str|None=Form(None),later_upload_filename:str|None=Form(None),name:str=Form('Change analysis'),location:str=Form(''),lat:str=Form(''),lon:str=Form(''),source:str=Form('USER UPLOAD'),before_date:str=Form(''),after_date:str=Form(''),u=Depends(current),s=Depends(db)):
 import cv2
 a,earlier_raw,earlier_name,earlier_type,earlier_key=resolve_image(earlier,earlier_upload_key,earlier_upload_filename,u); b,later_raw,later_name,later_type,later_key=resolve_image(later,later_upload_key,later_upload_filename,u); before_image=a.copy(); after_image=b.copy()
 if a.shape!=b.shape: b=cv2.resize(b,(a.shape[1],a.shape[0]))
 diff=np.mean(np.abs(a.astype(float)-b.astype(float)),axis=2); mask=diff>35; overlay=b.copy(); overlay[mask]=[255,70,50]
 source=source if source=='SAMPLE DATA' else 'USER UPLOAD'; result={'changed_percent':round(float(mask.mean()*100),1),'changed_pixels':int(mask.sum()),'compared_pixels':int(mask.size),'mean_pixel_difference':round(float(diff.mean()),1),'before_date':before_date or None,'after_date':after_date or None,'before_filename':earlier_name,'after_filename':later_name,'method':'RGB absolute pixel-difference heuristic with a per-pixel threshold after resizing to the earlier image dimensions (baseline; not a trained model).','source':source,'limitations':'Highlights pixel differences only; it does not prove geographic change. Images are resized, not georeferenced or co-registered. Shadows, clouds, seasonal variation, different viewpoints and acquisition conditions can appear as change. Compare similar views and interpret alongside context.','before_image':png_data(before_image),'after_image':png_data(after_image),'image':png_data(overlay)}
 x=save('change',name,location,lat,lon,result,u,s,[('earlier',earlier_name,earlier_type or 'application/octet-stream',earlier_raw,earlier_key),('later',later_name,later_type or 'application/octet-stream',later_raw,later_key)]); return {'id':x.id,'created':x.created.isoformat(),'result':public_result(x.id,u.id,result)}
@app.post('/api/analyze/flood')
def flood(file:UploadFile|None=File(None),before_file:UploadFile|None=File(None),after_file:UploadFile|None=File(None),file_upload_key:str|None=Form(None),before_file_upload_key:str|None=Form(None),after_file_upload_key:str|None=Form(None),file_upload_filename:str|None=Form(None),before_file_upload_filename:str|None=Form(None),after_file_upload_filename:str|None=Form(None),name:str=Form('Possible water detection'),location:str=Form(''),lat:str=Form(''),lon:str=Form(''),source:str=Form('USER UPLOAD'),image_date:str=Form(''),before_date:str=Form(''),after_date:str=Form(''),u=Depends(current),s:Session=Depends(db)):
    file_upload_key=file_upload_key if isinstance(file_upload_key,str) else None; before_file_upload_key=before_file_upload_key if isinstance(before_file_upload_key,str) else None; after_file_upload_key=after_file_upload_key if isinstance(after_file_upload_key,str) else None
    source=source if source=='SAMPLE DATA' else 'USER UPLOAD'
    if before_file is not None or after_file is not None or before_file_upload_key or after_file_upload_key:
        if (before_file is None and not before_file_upload_key) or (after_file is None and not after_file_upload_key):
            raise HTTPException(422,'Provide both Before Flood and After Flood images for comparison')
        before,before_raw,before_name,before_type,before_key=resolve_image(before_file,before_file_upload_key,before_file_upload_filename,u); after,after_raw,after_name,after_type,after_key=resolve_image(after_file,after_file_upload_key,after_file_upload_filename,u)
        result=flood_comparison(before,after)
        result.update({'before_date':before_date or None,'after_date':after_date or None,'before_filename':before_name,'after_filename':after_name,'source':source})
        original_images=[('before',before_name,before_type or 'application/octet-stream',before_raw,before_key),('after',after_name,after_type or 'application/octet-stream',after_raw,after_key)]
    elif file is not None or file_upload_key:
        im,raw,file_name,file_type,file_key=resolve_image(file,file_upload_key,file_upload_filename,u); m=flood_water_mask(im); water_pixels=int(m.sum()); total_pixels=int(m.size)
        color=np.array([255,35,180],dtype=np.float32); o=im.copy(); o[m]=(.45*o[m].astype(np.float32)+.55*color).astype(np.uint8)
        mask_rgb=np.zeros_like(im); mask_rgb[m]=color.astype(np.uint8)
        result={'analysis_mode':'single','possible_water_percent':round(water_pixels/total_pixels*100,2),'possible_water_pixels':water_pixels,'total_image_pixels':total_pixels,'area_estimate':None,'area_estimate_note':'Not calculated: this upload has no verified ground sampling distance and georeferenced pixel size. Coordinates or a place name alone are not enough to estimate area.','image_date':image_date or None,'filename':file_name,'method':'Multi-feature RGB, HSV and CIELAB water-screening heuristic with local texture and connected-region filtering (baseline; not a trained model).','source':source,'limitations':'Possible water only, not confirmed flooding. A single image cannot show whether water is new. Clouds, shadows, dark roofs, muddy soil and smooth agricultural fields can cause false detections or missed water. Compare matched pre-flood and post-flood imagery for stronger assessment.','image':png_data(o),'original_image':png_data(im),'mask_image':png_data(mask_rgb)}
        original_images=[('original',file_name,file_type or 'application/octet-stream',raw,file_key)]
    else:
        raise HTTPException(422,'Provide one image for single-image screening or both dates for flood comparison')
    x=save('flood',name,location,lat,lon,result,u,s,original_images)
    return {'id':x.id,'created':x.created.isoformat(),'result':public_result(x.id,u.id,result)}
@app.get('/api/history')
def history(u=Depends(current),s=Depends(db)): return [{'id':a.id,'kind':a.kind,'name':a.name,'location':a.location,'created':a.created.isoformat(),'result':public_result(a.id,u.id,json.loads(a.result))} for a in s.query(Analysis).filter_by(user_id=u.id).order_by(Analysis.created.desc()).limit(100)]
@app.get('/api/notifications')
def notifications(u=Depends(current),s=Depends(db)): return [{'id':n.id,'message':n.message,'created':n.created.isoformat()} for n in s.query(Notification).filter_by(user_id=u.id).order_by(Notification.created.desc()).limit(30)]
@app.get('/api/summary')
def summary(u=Depends(current),s=Depends(db)):
 rows=s.query(Analysis).filter_by(user_id=u.id).all(); return {'total':len(rows),'by_type':{k:sum(a.kind==k for a in rows) for k in ['land','change','flood']},'recent':[{'id':a.id,'kind':a.kind,'name':a.name,'created':a.created.isoformat()} for a in rows[-5:][::-1]]}
@app.get('/api/analysis/{aid}/previews/{field}')
def preview_image(aid:str,field:str,token:str,s:Session=Depends(db)):
 if not field.endswith('image'): raise HTTPException(404,'Preview not found')
 try: claim=jwt.decode(token,SECRET,algorithms=[ALG]); uid=int(claim['sub'])
 except Exception: raise HTTPException(401,'Invalid or expired preview link')
 if claim.get('analysis_id')!=aid or claim.get('field')!=field: raise HTTPException(403,'Preview link does not match this image')
 user=s.query(User).filter_by(id=uid,active=True).first(); analysis=s.query(Analysis).filter_by(id=aid,user_id=uid).first()
 if not user or not analysis: raise HTTPException(404,'Preview not found')
 value=json.loads(analysis.result).get(field)
 if isinstance(value,str) and value.startswith(OBJECT_REF_PREFIX.decode()):
  key=value[len(OBJECT_REF_PREFIX.decode()):]
  if not key.startswith(f'previews/{uid}/{aid}/'): raise HTTPException(404,'Preview not found')
  try: obj=object_store().get_object(Bucket=OBJECT_STORAGE_CONFIG['bucket'],Key=key)
  except Exception: raise HTTPException(404,'Preview not found')
  return StreamingResponse(stream_object(obj['Body']),media_type=obj.get('ContentType','image/png'),headers={'Cache-Control':'private, no-store'})
 if isinstance(value,str) and value.startswith('data:image/'):
  try: mime=value[5:].split(';',1)[0]; raw=base64.b64decode(value.split(',',1)[1],validate=True)
  except (ValueError,IndexError): raise HTTPException(404,'Preview not found')
  return StreamingResponse(io.BytesIO(raw),media_type=mime,headers={'Cache-Control':'private, no-store'})
 raise HTTPException(404,'Preview not found')
@app.get('/api/analysis/{aid}/report')
def report(aid:str,u=Depends(current),s:Session=Depends(db)):
    a=s.query(Analysis).filter_by(id=aid,user_id=u.id).first()
    if not a: raise HTTPException(404,'Analysis not found')
    data=json.loads(a.result); visuals={field:_preview_bytes(a.id,u.id,field,data.get(field)) for field in ('image','original_image','mask_image','before_image','after_image','before_mask_image','after_mask_image','change_mask_image')}; buf=io.BytesIO(); c=canvas.Canvas(buf); c.setTitle('SATQUERY Analysis Report')
    c.setFillColorRGB(.04,.12,.25); c.rect(0,735,612,57,fill=1); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',20); c.drawString(42,755,'SATQUERY  /  ANALYSIS REPORT')
    y=700; fields=[('Analysis ID',a.id),('Analysis type',a.kind.title()),('Analysis name',a.name),('Created',a.created.isoformat()),('Location',a.location or 'Not specified'),('Coordinates',f'{a.lat}, {a.lon}' if a.lat else 'Not specified'),('Data source',data.get('source','USER UPLOAD')),('Method',data.get('method',''))]
    for k,v in fields:
        c.setFillColorRGB(.08,.14,.23); c.setFont('Helvetica-Bold',10); c.drawString(42,y,k+':'); c.setFont('Helvetica',10); c.drawString(140,y,str(v)[:85]); y-=22
    y-=4; c.setFont('Helvetica-Bold',13); c.drawString(42,y,'Measured statistics'); y-=22
    for k,v in data.items():
        if k in ('image','original_image','mask_image','before_image','after_image','before_mask_image','after_mask_image','change_mask_image','method','source','limitations','area_estimate_note','alignment_warning') or v is None: continue
        c.setFont('Helvetica',10); c.drawString(48,y,f'{k.replace("_"," ").title()}: {v}'); y-=17
    y-=8; c.setFont('Helvetica-Bold',12); c.drawString(42,y,'Method and limitations'); y-=20; c.setFont('Helvetica',9)
    for paragraph in [data.get('method',''),data.get('limitations',''),data.get('alignment_warning',''),data.get('area_estimate_note','')]:
        for i in range(0,len(paragraph),95): c.drawString(42,y,paragraph[i:i+95]); y-=14
    if data.get('analysis_mode')=='comparison' and 'new_water_percent' in data and visuals.get('before_image') and visuals.get('after_image'):
        c.showPage(); c.setFillColorRGB(.04,.12,.25); c.rect(0,735,612,57,fill=1); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',18); c.drawString(42,755,'Flood comparison visual review')
        for x,key,label in [(42,'before_image','Before Flood'),(222,'after_image','After Flood'),(402,'image','New-water overlay')]:
            c.setFillColorRGB(.08,.14,.23); c.setFont('Helvetica-Bold',10); c.drawString(x,710,label)
            image_bytes=visuals.get(key); c.drawImage(ImageReader(io.BytesIO(image_bytes)),x,405,width=165,height=285,preserveAspectRatio=True,anchor='c')
        c.setFillColorRGB(.3,.37,.42); c.setFont('Helvetica',9); c.drawString(42,375,'Magenta marks pixels detected as newly water-covered by the heuristic when comparison is valid.')
    elif visuals.get('before_image') and visuals.get('after_image'):
        c.showPage(); c.setFillColorRGB(.04,.12,.25); c.rect(0,735,612,57,fill=1); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',18); c.drawString(42,755,'Change detection visual review')
        for x,key,label in [(42,'before_image','Before image'),(222,'after_image','After image'),(402,'image','Change overlay')]:
            c.setFillColorRGB(.08,.14,.23); c.setFont('Helvetica-Bold',10); c.drawString(x,710,label)
            image_bytes=visuals.get(key); c.drawImage(ImageReader(io.BytesIO(image_bytes)),x,405,width=165,height=285,preserveAspectRatio=True,anchor='c')
    elif visuals.get('original_image'):
        c.showPage(); c.setFillColorRGB(.04,.12,.25); c.rect(0,735,612,57,fill=1); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',18); c.drawString(42,755,'Flood screening visual review')
        for x,key,label in [(42,'original_image','Original image'),(222,'image','Detection overlay'),(402,'mask_image','Possible-water mask')]:
            c.setFillColorRGB(.08,.14,.23); c.setFont('Helvetica-Bold',10); c.drawString(x,710,label)
            image_bytes=visuals.get(key); c.drawImage(ImageReader(io.BytesIO(image_bytes)),x,405,width=165,height=285,preserveAspectRatio=True,anchor='c')
        c.setFillColorRGB(.3,.37,.42); c.setFont('Helvetica',9); c.drawString(42,375,'Magenta pixels indicate heuristic possible-water detections; review the limitations on page 1.')
    elif visuals.get('image'):
        c.showPage(); c.setFillColorRGB(.04,.12,.25); c.rect(0,735,612,57,fill=1); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',18); c.drawString(42,755,'Processed result preview')
        image_bytes=visuals['image']; c.drawImage(ImageReader(io.BytesIO(image_bytes)),42,230,width=528,height=475,preserveAspectRatio=True,anchor='c')
        c.setFillColorRGB(.3,.37,.42); c.setFont('Helvetica',9); c.drawString(42,205,'This is the processed output generated by the method described in this report.')
    else:
        c.showPage()
    c.save(); buf.seek(0); return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename=satquery-{aid[:8]}.pdf'})
@app.get('/api/analysis/{aid}/images/{role}/original')
def original_image(aid:str,role:str,u=Depends(current),s:Session=Depends(db)):
    analysis=s.query(Analysis).filter_by(id=aid,user_id=u.id).first()
    if not analysis: raise HTTPException(404,'Analysis not found')
    if role not in {'original','earlier','later','before','after'}: raise HTTPException(404,'Original upload is unavailable for this analysis')
    image=s.query(OriginalImage).filter_by(analysis_id=aid,role=role).first()
    if not image or not image.data: raise HTTPException(404,'Original upload is unavailable for this analysis')
    from urllib.parse import quote
    filename=Path(image.filename).name.replace('\\','_').replace('\r','_').replace('\n','_') or 'original-image'
    headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(filename)}",'Cache-Control':'private, no-store'}
    if image.data.startswith(OBJECT_REF_PREFIX):
        key=image.data[len(OBJECT_REF_PREFIX):].decode('utf-8')
        if not safe_upload_key(key,u.id): raise HTTPException(404,'Original upload is unavailable for this analysis')
        try: obj=object_store().get_object(Bucket=OBJECT_STORAGE_CONFIG['bucket'],Key=key)
        except Exception: raise HTTPException(404,'Original upload is unavailable for this analysis')
        return StreamingResponse(stream_object(obj['Body']),media_type=image.content_type or obj.get('ContentType','application/octet-stream'),headers=headers)
    return StreamingResponse(io.BytesIO(image.data),media_type=image.content_type,headers=headers)
@app.get('/api/admin/stats')
def admin_stats(u=Depends(admin),s=Depends(db)):
 return {'users':s.query(User).count(),'analyses':s.query(Analysis).count(),'uploads':sum(2 if a.kind=='change' else 1 for a in s.query(Analysis).all()),'recent':[{'email':u.email,'created':u.created.isoformat()} for u in s.query(User).order_by(User.created.desc()).limit(8)]}
@app.get('/api/admin/users')
def users(u=Depends(admin),s=Depends(db)): return [{'id':x.id,'email':x.email,'name':x.name,'role':x.role,'active':x.active} for x in s.query(User).all()]
@app.patch('/api/admin/users/{uid}')
def toggle(uid:int,u=Depends(admin),s=Depends(db)):
 x=s.query(User).filter_by(id=uid).first()
 if not x: raise HTTPException(404,'User not found')
 if x.role=='admin': raise HTTPException(400,'Admin accounts cannot be deactivated here')
 x.active=not x.active; s.commit(); return {'id':x.id,'active':x.active}
