import mimetypes

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

# 能在浏览器里执行的类型：一律按纯文本回。文件面板预览 HTML 走的是 srcDoc + sandbox，
# 不依赖这里的 MIME；直接点开 /file/raw 的人也不该在 API 源上跑仓库里的脚本。
_ACTIVE_SUFFIXES = frozenset(
    {".html", ".htm", ".xhtml", ".svg", ".xml", ".xsl", ".xslt", ".js", ".mjs", ".cjs", ".css"}
)

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.api.routes.projects import load_project
from blank_api.services.workspace import list_tree, mask_path, read_text_file, resolve_inside

router = APIRouter(prefix="/projects/{project_id}", tags=["files"], dependencies=[Depends(require_token)])


async def _workspace_root(db: DbSession, project_id: str) -> str:
    workspace = (await load_project(db, project_id)).active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    return workspace.path


@router.get("/tree")
async def get_tree(
    db: DbSession,
    project_id: str,
    path: str = Query(default=""),
    depth: int = Query(default=2, ge=1, le=6),
) -> dict:
    root = await _workspace_root(db, project_id)
    entries = list_tree(root, path, max_depth=depth)
    return {"root": mask_path(root), "path": path, "entries": [vars(e) for e in entries]}


@router.get("/file")
async def get_file(db: DbSession, project_id: str, path: str = Query(min_length=1)) -> dict:
    root = await _workspace_root(db, project_id)
    data = read_text_file(root, path)
    # Office 产物：文本层抽出来给结果面板看。抽不出来就保持原来的
    # 「二进制文件」回执，不把一次失败的预览伪装成空文件。
    if data.get("binary"):
        from blank_api.services.office_preview import extract_office, is_office

        if is_office(path):
            preview = extract_office(resolve_inside(root, path))
            if preview["text"]:
                return {
                    **data,
                    "binary": False,
                    "truncated": preview["truncated"],
                    "content": preview["text"],
                    "preview": "office",
                    "message": f"{preview['summary']}；文本预览，样式与图表已简化",
                }
    return data


@router.get("/file/raw")
async def get_file_raw(db: DbSession, project_id: str, path: str = Query(min_length=1)) -> FileResponse:
    root = await _workspace_root(db, project_id)
    target = resolve_inside(root, path)
    if target.is_dir():
        raise ApiError(code="is_a_directory", message="该路径是目录，无法作为文件读取", status_code=422)
    if target.suffix.lower() in _ACTIVE_SUFFIXES:
        media_type = "text/plain; charset=utf-8"
    else:
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name, content_disposition_type="inline")
