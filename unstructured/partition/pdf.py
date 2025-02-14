import os
import re
import warnings
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Iterator, List, Optional, Union, cast

import pdf2image
import PIL
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTContainer, LTImage, LTItem, LTTextBox
from pdfminer.utils import open_filename

from unstructured.cleaners.core import clean_extra_whitespace
from unstructured.documents.coordinates import PixelSpace, PointSpace
from unstructured.documents.elements import (
    CoordinatesMetadata,
    Element,
    ElementMetadata,
    Image,
    PageBreak,
    Text,
    process_metadata,
)
from unstructured.file_utils.filetype import (
    FileType,
    add_metadata_with_filetype,
)
from unstructured.nlp.patterns import PARAGRAPH_PATTERN
from unstructured.partition.common import (
    convert_to_bytes,
    document_to_element_list,
    exactly_one,
    get_last_modified_date,
    get_last_modified_date_from_file,
    spooled_to_bytes_io_if_needed,
)
from unstructured.partition.strategies import determine_pdf_or_image_strategy
from unstructured.partition.text import element_from_text, partition_text
from unstructured.partition.utils.constants import SORT_MODE_BASIC, SORT_MODE_XY_CUT
from unstructured.partition.utils.sorting import sort_page_elements
from unstructured.utils import requires_dependencies

RE_MULTISPACE_INCLUDING_NEWLINES = re.compile(pattern=r"\s+", flags=re.DOTALL)


@process_metadata()
@add_metadata_with_filetype(FileType.PDF)
def partition_pdf(
    filename: str = "",
    file: Optional[Union[BinaryIO, SpooledTemporaryFile]] = None,
    include_page_breaks: bool = False,
    strategy: str = "auto",
    infer_table_structure: bool = False,
    ocr_languages: str = "eng",
    max_partition: Optional[int] = 1500,
    min_partition: Optional[int] = 0,
    include_metadata: bool = True,
    metadata_filename: Optional[str] = None,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
) -> List[Element]:
    """Parses a pdf document into a list of interpreted elements.
    Parameters
    ----------
    filename
        A string defining the target filename path.
    file
        A file-like object as bytes --> open(filename, "rb").
    strategy
        The strategy to use for partitioning the PDF. Valid strategies are "hi_res",
        "ocr_only", and "fast". When using the "hi_res" strategy, the function uses
        a layout detection model to identify document elements. When using the
        "ocr_only" strategy, partition_pdf simply extracts the text from the
        document using OCR and processes it. If the "fast" strategy is used, the text
        is extracted directly from the PDF. The default strategy `auto` will determine
        when a page can be extracted using `fast` mode, otherwise it will fall back to `hi_res`.
    infer_table_structure
        Only applicable if `strategy=hi_res`.
        If True, any Table elements that are extracted will also have a metadata field
        named "text_as_html" where the table's text content is rendered into an html string.
        I.e., rows and cells are preserved.
        Whether True or False, the "text" field is always present in any Table element
        and is the text content of the table (no structure).
    ocr_languages
        The languages to use for the Tesseract agent. To use a language, you'll first need
        to isntall the appropriate Tesseract language pack.
    max_partition
        The maximum number of characters to include in a partition. If None is passed,
        no maximum is applied. Only applies to the "ocr_only" strategy.
    min_partition
        The minimum number of characters to include in a partition. Only applies if
        processing text/plain content.
    metadata_last_modified
        The last modified date for the document.
    """
    exactly_one(filename=filename, file=file)
    return partition_pdf_or_image(
        filename=filename,
        file=file,
        include_page_breaks=include_page_breaks,
        strategy=strategy,
        infer_table_structure=infer_table_structure,
        ocr_languages=ocr_languages,
        max_partition=max_partition,
        min_partition=min_partition,
        metadata_last_modified=metadata_last_modified,
        **kwargs,
    )


def extractable_elements(
    filename: str = "",
    file: Optional[Union[bytes, BinaryIO, SpooledTemporaryFile]] = None,
    include_page_breaks: bool = False,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
):
    return _partition_pdf_with_pdfminer(
        filename=filename,
        file=file,
        include_page_breaks=include_page_breaks,
        metadata_last_modified=metadata_last_modified,
        **kwargs,
    )


def get_the_last_modification_date_pdf_or_img(
    file: Optional[Union[bytes, BinaryIO, SpooledTemporaryFile]] = None,
    filename: Optional[str] = "",
) -> Union[str, None]:
    last_modification_date = None
    if not file and filename:
        last_modification_date = get_last_modified_date(filename=filename)
    elif not filename and file:
        last_modification_date = get_last_modified_date_from_file(file=file)
    return last_modification_date


def partition_pdf_or_image(
    filename: str = "",
    file: Optional[Union[bytes, BinaryIO, SpooledTemporaryFile]] = None,
    is_image: bool = False,
    include_page_breaks: bool = False,
    strategy: str = "auto",
    infer_table_structure: bool = False,
    ocr_languages: str = "eng",
    max_partition: Optional[int] = 1500,
    min_partition: Optional[int] = 0,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
) -> List[Element]:
    """Parses a pdf or image document into a list of interpreted elements."""
    # TODO(alan): Extract information about the filetype to be processed from the template
    # route. Decoding the routing should probably be handled by a single function designed for
    # that task so as routing design changes, those changes are implemented in a single
    # function.

    last_modification_date = get_the_last_modification_date_pdf_or_img(
        file=file,
        filename=filename,
    )

    if (
        not is_image
        and determine_pdf_or_image_strategy(
            strategy,
            filename=filename,
            file=file,
            is_image=is_image,
            infer_table_structure=infer_table_structure,
        )
        != "ocr_only"
    ):
        extracted_elements = extractable_elements(
            filename=filename,
            file=spooled_to_bytes_io_if_needed(file),
            include_page_breaks=include_page_breaks,
            metadata_last_modified=metadata_last_modified or last_modification_date,
            **kwargs,
        )
        pdf_text_extractable = any(
            isinstance(el, Text) and el.text.strip() for el in extracted_elements
        )
    else:
        pdf_text_extractable = False

    strategy = determine_pdf_or_image_strategy(
        strategy,
        filename=filename,
        file=file,
        is_image=is_image,
        infer_table_structure=infer_table_structure,
        pdf_text_extractable=pdf_text_extractable,
    )

    if strategy == "hi_res":
        # NOTE(robinson): Catches a UserWarning that occurs when detectron is called
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            layout_elements = _partition_pdf_or_image_local(
                filename=filename,
                file=spooled_to_bytes_io_if_needed(file),
                is_image=is_image,
                infer_table_structure=infer_table_structure,
                include_page_breaks=include_page_breaks,
                ocr_languages=ocr_languages,
                ocr_mode="entire_page",
                metadata_last_modified=metadata_last_modified or last_modification_date,
                **kwargs,
            )

    elif strategy == "fast":
        return extracted_elements

    elif strategy == "ocr_only":
        # NOTE(robinson): Catches file conversion warnings when running with PDFs
        with warnings.catch_warnings():
            return _partition_pdf_or_image_with_ocr(
                filename=filename,
                file=file,
                include_page_breaks=include_page_breaks,
                ocr_languages=ocr_languages,
                is_image=is_image,
                max_partition=max_partition,
                min_partition=min_partition,
                metadata_last_modified=metadata_last_modified or last_modification_date,
            )

    return layout_elements


@requires_dependencies("unstructured_inference")
def _partition_pdf_or_image_local(
    filename: str = "",
    file: Optional[Union[bytes, BinaryIO]] = None,
    is_image: bool = False,
    infer_table_structure: bool = False,
    include_page_breaks: bool = False,
    ocr_languages: str = "eng",
    ocr_mode: str = "entire_page",
    model_name: Optional[str] = None,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
) -> List[Element]:
    """Partition using package installed locally."""
    from unstructured_inference.inference.layout import (
        process_data_with_model,
        process_file_with_model,
    )

    model_name = model_name if model_name else os.environ.get("UNSTRUCTURED_HI_RES_MODEL_NAME")
    if file is None:
        pdf_image_dpi = kwargs.pop("pdf_image_dpi", None)
        process_file_with_model_kwargs = {
            "is_image": is_image,
            "ocr_languages": ocr_languages,
            "ocr_mode": ocr_mode,
            "extract_tables": infer_table_structure,
            "model_name": model_name,
        }
        if pdf_image_dpi:
            process_file_with_model_kwargs["pdf_image_dpi"] = pdf_image_dpi
        layout = process_file_with_model(
            filename,
            **process_file_with_model_kwargs,
        )
    else:
        layout = process_data_with_model(
            file,
            is_image=is_image,
            ocr_languages=ocr_languages,
            ocr_mode=ocr_mode,
            extract_tables=infer_table_structure,
            model_name=model_name,
        )
    elements = document_to_element_list(
        layout,
        sortable=True,
        include_page_breaks=include_page_breaks,
        last_modification_date=metadata_last_modified,
        # NOTE(crag): do not attempt to derive ListItem's from a layout-recognized "List"
        # block with NLP rules. Otherwise, the assumptions in
        # unstructured.partition.common::layout_list_to_list_items often result in weird chunking.
        infer_list_items=False,
        **kwargs,
    )
    out_elements = []

    for el in elements:
        if (isinstance(el, PageBreak) and not include_page_breaks) or (
            # NOTE(crag): small chunks of text from Image elements tend to be garbage
            isinstance(el, Image)
            and (el.text is None or len(el.text) < 24 or el.text.find(" ") == -1)
        ):
            continue
        # NOTE(crag): this is probably always a Text object, but check for the sake of typing
        if isinstance(el, Text):
            el.text = re.sub(
                RE_MULTISPACE_INCLUDING_NEWLINES,
                " ",
                el.text or "",
            ).strip()
            if el.text or isinstance(el, PageBreak):
                out_elements.append(cast(Element, el))

    return out_elements


@requires_dependencies("pdfminer", "local-inference")
def _partition_pdf_with_pdfminer(
    filename: str = "",
    file: Optional[BinaryIO] = None,
    include_page_breaks: bool = False,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
) -> List[Element]:
    """Partitions a PDF using PDFMiner instead of using a layoutmodel. Used for faster
    processing or detectron2 is not available.

    Implementation is based on the `extract_text` implemenation in pdfminer.six, but
    modified to support tracking page numbers and working with file-like objects.

    ref: https://github.com/pdfminer/pdfminer.six/blob/master/pdfminer/high_level.py
    """
    exactly_one(filename=filename, file=file)
    if filename:
        with open_filename(filename, "rb") as fp:
            fp = cast(BinaryIO, fp)
            elements = _process_pdfminer_pages(
                fp=fp,
                filename=filename,
                include_page_breaks=include_page_breaks,
                metadata_last_modified=metadata_last_modified,
                **kwargs,
            )

    elif file:
        fp = cast(BinaryIO, file)
        elements = _process_pdfminer_pages(
            fp=fp,
            filename=filename,
            include_page_breaks=include_page_breaks,
            metadata_last_modified=metadata_last_modified,
            **kwargs,
        )

    return elements


def _extract_text(item: LTItem) -> str:
    """Recursively extracts text from PDFMiner objects to account
    for scenarios where the text is in a sub-container."""
    if hasattr(item, "get_text"):
        return item.get_text()

    elif isinstance(item, LTContainer):
        text = ""
        for child in item:
            text += _extract_text(child) or ""
        return text

    elif isinstance(item, (LTTextBox, LTImage)):
        # TODO(robinson) - Support pulling text out of images
        # https://github.com/pdfminer/pdfminer.six/blob/master/pdfminer/image.py#L90
        return "\n"
    return "\n"


def _process_pdfminer_pages(
    fp: BinaryIO,
    filename: str = "",
    include_page_breaks: bool = False,
    metadata_last_modified: Optional[str] = None,
    **kwargs,
):
    """Uses PDF miner to split a document into pages and process them."""
    elements: List[Element] = []
    sort_mode = kwargs.get("sort_mode", SORT_MODE_XY_CUT)

    for i, page in enumerate(extract_pages(fp)):  # type: ignore
        width, height = page.width, page.height

        text_segments = []
        page_elements = []
        for obj in page:
            x1, y2, x2, y1 = obj.bbox
            y1 = height - y1
            y2 = height - y2

            if hasattr(obj, "get_text"):
                _text_snippets = [obj.get_text()]
            else:
                _text = _extract_text(obj)
                _text_snippets = re.split(PARAGRAPH_PATTERN, _text)

            for _text in _text_snippets:
                _text = clean_extra_whitespace(_text)
                if _text.strip():
                    text_segments.append(_text)
                    coordinate_system = PixelSpace(
                        width=width,
                        height=height,
                    )
                    points = ((x1, y1), (x1, y2), (x2, y2), (x2, y1))
                    element = element_from_text(
                        _text,
                        coordinates=points,
                        coordinate_system=coordinate_system,
                    )
                    coordinates_metadata = CoordinatesMetadata(
                        points=points,
                        system=coordinate_system,
                    )
                    element.metadata = ElementMetadata(
                        filename=filename,
                        page_number=i + 1,
                        coordinates=coordinates_metadata,
                        last_modified=metadata_last_modified,
                    )
                    page_elements.append(element)

        sorted_page_elements = sort_page_elements(page_elements, SORT_MODE_BASIC)
        if sort_mode != SORT_MODE_BASIC:
            sorted_page_elements = sort_page_elements(sorted_page_elements, sort_mode)
        elements += sorted_page_elements

        if include_page_breaks:
            elements.append(PageBreak(text=""))

    return elements


def convert_pdf_to_images(
    filename: str = "",
    file: Optional[Union[bytes, BinaryIO, SpooledTemporaryFile]] = None,
    chunk_size: int = 10,
) -> Iterator[PIL.Image.Image]:
    # Convert a PDF in small chunks of pages at a time (e.g. 1-10, 11-20... and so on)
    exactly_one(filename=filename, file=file)
    if file is not None:
        f_bytes = convert_to_bytes(file)
        info = pdf2image.pdfinfo_from_bytes(f_bytes)
    else:
        f_bytes = None
        info = pdf2image.pdfinfo_from_path(filename)

    total_pages = info["Pages"]
    for start_page in range(1, total_pages + 1, chunk_size):
        end_page = min(start_page + chunk_size - 1, total_pages)
        if f_bytes is not None:
            chunk_images = pdf2image.convert_from_bytes(
                f_bytes,
                first_page=start_page,
                last_page=end_page,
            )
        else:
            chunk_images = pdf2image.convert_from_path(
                filename,
                first_page=start_page,
                last_page=end_page,
            )

        for image in chunk_images:
            yield image


def add_pytesseract_bbox_to_elements(elements, bboxes, width, height):
    """
    Get the bounding box of each element and add it to element.metadata.coordinates

    Args:
        elements: elements containing text detected by pytesseract.image_to_string.
        bboxes (str): The return value of pytesseract.image_to_boxes.
    """
    # (NOTE) jennings: This function was written with pytesseract in mind, but
    # paddle returns similar values via `ocr.ocr(img)`.
    # See more at issue #1176: https://github.com/Unstructured-IO/unstructured/issues/1176
    min_x = float("inf")
    min_y = float("inf")
    max_x = 0
    max_y = 0
    point_space = PointSpace(
        width=width,
        height=height,
    )
    pixel_space = PixelSpace(
        width=width,
        height=height,
    )

    boxes = bboxes.strip().split("\n")
    i = 0
    for element in elements:
        char_count = len(element.text.replace(" ", ""))

        for box in boxes[i : i + char_count]:  # noqa
            _, x1, y1, x2, y2, _ = box.split()
            x1, y1, x2, y2 = map(int, [x1, y1, x2, y2])

            min_x = min(min_x, x1)
            min_y = min(min_y, y1)
            max_x = max(max_x, x2)
            max_y = max(max_y, y2)

        points = ((min_x, min_y), (min_x, max_y), (max_x, max_y), (max_x, min_y))
        converted_points = []
        for point in points:
            x, y = point
            new_x, new_y = point_space.convert_coordinates_to_new_system(pixel_space, x, y)
            converted_points.append((new_x, new_y))

        element.metadata.coordinates = CoordinatesMetadata(
            points=converted_points,
            system=pixel_space,
        )

        # reset for next element
        min_x = float("inf")
        min_y = float("inf")
        max_x = 0
        max_y = 0
        i += char_count

    return elements


@requires_dependencies("pytesseract")
def _partition_pdf_or_image_with_ocr(
    filename: str = "",
    file: Optional[Union[bytes, BinaryIO, SpooledTemporaryFile]] = None,
    include_page_breaks: bool = False,
    ocr_languages: str = "eng",
    is_image: bool = False,
    max_partition: Optional[int] = 1500,
    min_partition: Optional[int] = 0,
    metadata_last_modified: Optional[str] = None,
):
    """Partitions an image or PDF using Tesseract OCR. For PDFs, each page is converted
    to an image prior to processing."""
    import pytesseract

    if is_image:
        if file is not None:
            image = PIL.Image.open(file)
            text = pytesseract.image_to_string(image, config=f"-l '{ocr_languages}'")
            bboxes = pytesseract.image_to_boxes(image, config=f"-l '{ocr_languages}'")
        else:
            image = PIL.Image.open(filename)
            text = pytesseract.image_to_string(filename, config=f"-l '{ocr_languages}'")
            bboxes = pytesseract.image_to_boxes(filename, config=f"-l '{ocr_languages}'")
        elements = partition_text(
            text=text,
            max_partition=max_partition,
            min_partition=min_partition,
            metadata_last_modified=metadata_last_modified,
        )
        width, height = image.size
        add_pytesseract_bbox_to_elements(elements, bboxes, width, height)

    else:
        elements = []
        page_number = 0
        for image in convert_pdf_to_images(filename, file):
            page_number += 1
            metadata = ElementMetadata(
                filename=filename,
                page_number=page_number,
                last_modified=metadata_last_modified,
            )
            text = pytesseract.image_to_string(image, config=f"-l '{ocr_languages}'")
            bboxes = pytesseract.image_to_boxes(image, config=f"-l '{ocr_languages}'")
            width, height = image.size

            _elements = partition_text(
                text=text,
                max_partition=max_partition,
                min_partition=min_partition,
            )
            for element in _elements:
                element.metadata = metadata
                elements.append(element)

            add_pytesseract_bbox_to_elements(elements, bboxes, width, height)

            if include_page_breaks:
                elements.append(PageBreak(text=""))
    return elements
