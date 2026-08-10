# autoblog — v22.3(2.x): 별도 블로그 앱
# FastAPI + static, 별도 Vercel + 별도 Supabase + 별도 도메인.
# SEO·AEO·GEO·GA4 설계 원칙 (문서 11 Phase 2).
import os

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local")


def load_config():
    return {
        # 발행 API 토큰 (autostudio 발행 클라이언트가 사용)
        "blog_token": os.getenv("BLOG_TOKEN", ""),
        # GA4 Measurement ID (env — 미설정 시 스크립트 미삽입)
        "ga4_measurement_id": os.getenv("GA4_MEASUREMENT_ID", ""),
        # 공개 base URL (OG·canonical 절대 URL — 도메인 확정 전 Vercel 기본 도메인)
        "base_url": os.getenv("BLOG_BASE_URL", "http://localhost:8000").rstrip("/"),
        "db_url": os.getenv("DATABASE_URL", "sqlite:///data/blog.db"),
        "env": os.getenv("ENV", "development").strip().lower(),
        # 발행 API CORS 허용 origin (콤마 구분 — autostudio의 Vercel/로컬 주소)
        "cors_origins": os.getenv("CORS_ORIGINS", ""),
    }
