"""
WebUI Storyboard View & Interactive Scene Review Renderer

Renders the visual storyboard cards in Streamlit, allowing users to:
1. Review each scene's thumbnail, duration, exact speech timing, and narration.
2. Edit subtitle/script text per scene.
3. Switch or regenerate scene media before final FFmpeg rendering:
   - Generate high-fidelity AI images with Ken Burns animation using LLM prompt suggestions.
   - Search and select stock video footage from Pexels.
4. Trigger the final video compilation once satisfied.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from loguru import logger
import streamlit as st

from app.models.schema import VideoAspect
from app.services import ai_image, material
from webui.components.storyboard import (
    StoryboardDraft,
    generate_scene_image_prompt,
    generate_scene_thumbnail,
    load_storyboard_draft,
    save_storyboard_draft,
    update_scene_material,
    update_scene_text,
)


def _search_stock_videos_safe(
    search_term: str,
    minimum_duration: int = 3,
    aspect: VideoAspect = VideoAspect.portrait,
    max_results: int = 4,
) -> list[dict[str, Any]]:
    """Safe wrapper to search stock videos without crashing if API key is not configured."""
    results: list[dict[str, Any]] = []
    clean_term = search_term.strip()
    if not clean_term:
        return results

    try:
        found = material.search_videos_pexels(
            clean_term,
            minimum_duration=minimum_duration,
            video_aspect=aspect,
        )
        for item in found[:max_results]:
            results.append({
                "url": item.url,
                "duration": item.duration,
                "provider": "pexels",
            })
    except Exception as exc:
        logger.debug(f"stock video search failed for {clean_term!r}: {exc}")

    return results


def render_storyboard_panel(
    task_id: str,
    on_render_callback: Callable[[StoryboardDraft], None] | None = None,
    on_discard_callback: Callable[[str], None] | None = None,
) -> StoryboardDraft | None:
    """
    Renders the interactive Storyboard review panel for task_id.
    Returns the updated StoryboardDraft if available.
    """
    draft = load_storyboard_draft(task_id)
    if not draft:
        st.info(f"Nenhum rascunho de storyboard encontrado para a tarefa {task_id}.")
        return None

    st.markdown("### 🎬 Storyboard — Revisão de Rascunho")
    st.caption(
        f"**Assunto:** {draft.video_subject} | "
        f"**Duração Total:** {draft.total_duration:.1f}s | "
        f"**Cenas:** {len(draft.scenes)}"
    )

    draft_modified = False

    for scene in draft.scenes:
        with st.container():
            col_media, col_details = st.columns([1, 2])

            with col_media:
                if scene.end_time > scene.start_time:
                    s_min, s_sec = int(scene.start_time // 60), int(scene.start_time % 60)
                    e_min, e_sec = int(scene.end_time // 60), int(scene.end_time % 60)
                    timing_str = f"{s_min:02d}:{s_sec:02d} - {e_min:02d}:{e_sec:02d}"
                    st.markdown(f"**Cena {scene.scene_index}** ({scene.duration:.1f}s | `{timing_str}`)")
                else:
                    st.markdown(f"**Cena {scene.scene_index}** ({scene.duration:.1f}s)")

                thumb = scene.thumbnail_path
                if not thumb or not os.path.exists(thumb):
                    if scene.material_path and os.path.exists(scene.material_path):
                        task_dir = os.path.dirname(scene.material_path)
                        target_thumb = os.path.join(
                            task_dir, f"scene-{scene.scene_index}-preview.jpg"
                        )
                        thumb = generate_scene_thumbnail(scene.material_path, target_thumb) or ""
                        if thumb:
                            scene.thumbnail_path = thumb
                            draft_modified = True

                if thumb and os.path.exists(thumb):
                    st.image(thumb, use_container_width=True)
                elif scene.material_path and os.path.exists(scene.material_path):
                    st.video(scene.material_path)
                else:
                    st.warning("Mídia não encontrada")

                caption_parts = [f"Fonte: `{scene.material_provider or 'stock'}`"]
                if scene.search_term:
                    caption_parts.append(f"Tema: `{scene.search_term}`")
                st.caption(" | ".join(caption_parts))

            with col_details:
                new_text = st.text_area(
                    f"Texto da Cena {scene.scene_index}",
                    value=scene.text,
                    key=f"sb_text_{task_id}_{scene.scene_index}",
                    height=80,
                )
                if new_text.strip() != scene.text.strip():
                    update_scene_text(draft, scene.scene_index, new_text)
                    draft_modified = True

                with st.expander("Trocar / Regerar Mídia", expanded=False):
                    tab_ai, tab_stock = st.tabs(["🎨 Gerar com IA (Ken Burns)", "🔍 Buscar Banco de Vídeos"])

                    with tab_ai:
                        ai_query_key = f"sb_ai_query_{task_id}_{scene.scene_index}"
                        if ai_query_key not in st.session_state:
                            st.session_state[ai_query_key] = scene.search_term or ""

                        col_input, col_sug = st.columns([3, 1])
                        with col_sug:
                            st.write("")
                            if st.button(
                                "✨ Sugerir Prompt",
                                key=f"sb_btn_sug_{task_id}_{scene.scene_index}",
                                help="Usa IA para descrever uma cena cinematográfica em inglês baseada na narração",
                            ):
                                with st.spinner("Criando prompt visual com IA..."):
                                    suggested = generate_scene_image_prompt(scene.text, scene.search_term)
                                    st.session_state[ai_query_key] = suggested
                                    st.rerun()

                        with col_input:
                            prompt_val = st.text_input(
                                "Prompt da Imagem (em inglês)",
                                key=ai_query_key,
                                help="Descreva a cena visual em inglês ou use o botão ✨ Sugerir Prompt",
                            )

                        auto_refine = st.checkbox(
                            "Otimizar prompt automaticamente com IA (estilo Flux/Midjourney)",
                            value=True,
                            key=f"sb_refine_{task_id}_{scene.scene_index}",
                        )

                        if st.button(
                            "🎨 Gerar Imagem com IA",
                            key=f"sb_btn_ai_{task_id}_{scene.scene_index}",
                            type="primary",
                            help="Gera uma imagem hiper-realista com IA e aplica movimento Ken Burns na duração exata da cena",
                        ):
                            with st.spinner("Gerando imagem com IA e animando com Ken Burns..."):
                                prompt_to_use = prompt_val.strip()
                                if auto_refine:
                                    prompt_to_use = generate_scene_image_prompt(
                                        scene.text, prompt_to_use or scene.search_term
                                    )
                                elif not prompt_to_use:
                                    prompt_to_use = generate_scene_image_prompt(
                                        scene.text, scene.search_term
                                    )

                                item = ai_image.generate_ai_video_clip(
                                    prompt=prompt_to_use,
                                    duration=scene.duration,
                                    video_aspect=VideoAspect.portrait,
                                )
                                if item and item.url:
                                    saved = material.save_video(item.url)
                                    if saved:
                                        update_scene_material(
                                            draft,
                                            scene.scene_index,
                                            new_material_path=saved,
                                            new_provider="ai_image",
                                            new_search_term=prompt_to_use,
                                        )
                                        draft_modified = True
                                        save_storyboard_draft(task_id, draft)
                                        st.success("Mídia atualizada com sucesso!")
                                        st.rerun()
                                    else:
                                        st.error("Erro ao salvar o clipe gerado.")
                                else:
                                    st.error("Falha ao gerar imagem com IA. Tente outro prompt.")

                    with tab_stock:
                        stock_query_key = f"sb_stock_query_{task_id}_{scene.scene_index}"
                        stock_results_key = f"sb_stock_results_{task_id}_{scene.scene_index}"
                        if stock_query_key not in st.session_state:
                            st.session_state[stock_query_key] = scene.search_term or ""

                        col_sq, col_sbtn = st.columns([3, 1])
                        with col_sbtn:
                            st.write("")
                            if st.button(
                                "🔍 Buscar",
                                key=f"sb_btn_stock_{task_id}_{scene.scene_index}",
                            ):
                                term_to_search = st.session_state.get(stock_query_key, "").strip()
                                with st.spinner("Buscando vídeos no Pexels..."):
                                    found = _search_stock_videos_safe(
                                        term_to_search,
                                        minimum_duration=max(1, int(scene.duration)),
                                        aspect=VideoAspect.portrait,
                                        max_results=4,
                                    )
                                    st.session_state[stock_results_key] = found
                                    st.rerun()

                        with col_sq:
                            st.text_input(
                                "Termo de busca para vídeo de estoque (em inglês)",
                                key=stock_query_key,
                            )

                        results = st.session_state.get(stock_results_key, [])
                        if results:
                            st.write(f"**Resultados encontrados ({len(results)}):**")
                            res_cols = st.columns(min(len(results), 2))
                            for r_idx, res_item in enumerate(results):
                                with res_cols[r_idx % len(res_cols)]:
                                    st.video(res_item["url"])
                                    st.caption(f"Duração: {res_item['duration']}s")
                                    if st.button(
                                        f"Usar Vídeo #{r_idx + 1}",
                                        key=f"sb_pick_{task_id}_{scene.scene_index}_{r_idx}",
                                    ):
                                        with st.spinner("Baixando vídeo selecionado..."):
                                            saved_path = material.save_video(res_item["url"])
                                            if saved_path:
                                                update_scene_material(
                                                    draft,
                                                    scene.scene_index,
                                                    new_material_path=saved_path,
                                                    new_provider=res_item.get("provider", "pexels"),
                                                    new_search_term=st.session_state.get(stock_query_key, ""),
                                                )
                                                draft_modified = True
                                                save_storyboard_draft(task_id, draft)
                                                st.success("Vídeo selecionado com sucesso!")
                                                st.rerun()

            st.divider()

    if draft_modified:
        save_storyboard_draft(task_id, draft)

    col_action1, col_action2 = st.columns([2, 1])
    with col_action1:
        if st.button("🚀 Aprovar e Renderizar Vídeo Final", type="primary", use_container_width=True):
            if on_render_callback:
                on_render_callback(draft)
    with col_action2:
        if st.button("Descartar Rascunho", use_container_width=True):
            if on_discard_callback:
                on_discard_callback(task_id)

    return draft
