"""附件上传与回读（图片、文本类、PDF）。

用 base64 JSON 而不是 multipart：后者要在服务端多一个 ``python-multipart`` 依赖，
打包链、本项目 venv、安装包里那份运行时都要跟着同步一次，而这条链路上任何一环
漏掉都表现为"上传按钮点了没反应"。文件本来就在浏览器内存里，用 JSON 发过来
省掉整条依赖链，代价是请求体大约 1.37 倍——单份限 8MB，付得起。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.deps import DbSession
from blank_api.services.uploads import UploadError, UploadStore, decode_payload

router = APIRouter(prefix="/projects", tags=["uploads"], dependencies=[Depends(require_token)])


class UploadIn(BaseModel):
    name: str = Field(default="", max_length=200)
    mime: str = Field(default="", max_length=100)
    # base64，允许带 data URL 前缀（截图工具直接喂过来的就是那种）。
    data: str = Field(min_length=1)


class UploadOut(BaseModel):
    id: str
    name: str
    mime: str
    size: int
    # image / text / pdf：界面据此决定渲染缩略图还是文件条。
    kind: str


@router.post("/{project_id}/uploads", response_model=UploadOut, status_code=201)
async def create_upload(project_id: str, payload: UploadIn, db: DbSession) -> UploadOut:
    # 先确认项目存在：附件是按项目分目录的，项目不在了写进去就是孤儿文件。
    await load_project(db, project_id)
    try:
        ref = UploadStore().save(project_id, payload.name, payload.mime, decode_payload(payload.data))
    except UploadError as exc:
        raise ApiError(code="upload_invalid", message=str(exc), status_code=422) from exc
    return UploadOut(**ref.as_dict(), kind=ref.kind)


@router.get("/{project_id}/uploads/{upload_id}")
async def read_upload(project_id: str, upload_id: str, db: DbSession) -> FileResponse:
    await load_project(db, project_id)
    ref = UploadStore().find(project_id, upload_id)
    if ref is None:
        raise ApiError(code="upload_not_found", message="附件不存在或已被清理", status_code=404)
    # inline：这是给界面上 <img> 直接用的，不是下载链接。
    return FileResponse(ref.path, media_type=ref.mime, filename=ref.name, content_disposition_type="inline")
