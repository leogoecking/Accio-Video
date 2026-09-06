"""
WebUI Storyboard View & Interactive Scene Review Renderer

Renders the visual storyboard cards in Streamlit, allowing users to:
1. Review each scene's thumbnail and assigned narration.
2. Edit subtitle/script text per scene.
3. Switch or regenerate scene media before final FFmpeg rendering.
4. Trigger the final video compilation once satisfied.
"""
from __future__ import annotations

import os
from typing import Callable

import streamlit as st

from app.models.schema import VideoAspect
from app.services import ai_image, material
from webui.components.storyboard import (
    StoryboardDraft,
    generate_scene_thumbnail,
    load_storyboard_draft,
    save_storyboard_draft,
    update_scene_material,
    update_scene_text,
)


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
        f"**Duração Estimada:** {draft.total_duration:.1f}s | "
        f"**Cenas:** {len(draft.scenes)}"
    )

    draft_modified = False

    for scene in draft.scenes:
        with st.container():
            col_media, col_details = st.columns([1, 2])

            with col_media:
                st.markdown(f"**Cena {scene.scene_index}** ({scene.duration:.1f}s)")
                thumb = scene.thumbnail_path
                if not thumb or not os.path.exists(thumb):
                    if scene.material_path and os.path.exists(scene.material_path):
                        task_dir = os.path.dirname(scene.material_path)
                        target_thumb = os.path.join(task_dir, f"scene-{scene.scene_index}-preview.jpg")
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

                st.caption(f"Fonte: `{scene.material_provider or 'stock'}`")

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
                    search_col, action_col = st.columns([2, 1])
                    with search_col:
                        custom_query = st.text_input(
                            "Termo visual",
                            value=scene.search_term or scene.text[:30],
                            key=f"sb_query_{task_id}_{scene.scene_index}",
                        )
                    with action_col:
                        if st.button(
                            "Gerar com IA",
                            key=f"sb_btn_ai_{task_id}_{scene.scene_index}",
                            help="Gera uma nova imagem com IA para esta cena",
                        ):
                            with st.spinner("Gerando imagem IA..."):
                                item = ai_image.generate_ai_video_clip(
                                    prompt=custom_query,
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
                                            new_search_term=custom_query,
                                        )
                                        draft_modified = True
                                        st.success("Mídia atualizada!")
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
