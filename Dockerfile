# syntax=docker/dockerfile:experimental
FROM quay.io/unstructured-io/base-images:rocky8.7-5 as base

ARG PIP_VERSION

# Set up environment
ENV HOME /home/
WORKDIR ${HOME}
RUN mkdir ${HOME}/.ssh && chmod go-rwx ${HOME}/.ssh \
  &&  ssh-keyscan -t rsa github.com >> /home/.ssh/known_hosts
ENV PYTHONPATH="${PYTHONPATH}:${HOME}"
ENV PATH="/home/usr/.local/bin:${PATH}"

FROM base as deps
# Copy and install Unstructured
COPY requirements requirements

RUN python3.10 -m pip install pip==${PIP_VERSION} && \
  dnf -y groupinstall "Development Tools" && \
  pip install --no-cache -r requirements/base.txt && \
  pip install --no-cache -r requirements/test.txt && \
  pip install --no-cache -r requirements/huggingface.txt && \
  pip install --no-cache -r requirements/dev.txt && \
  pip install --no-cache -r requirements/ingest-box.txt && \
  pip install --no-cache -r requirements/ingest-confluence.txt && \
  pip install --no-cache -r requirements/ingest-discord.txt && \
  pip install --no-cache -r requirements/ingest-dropbox.txt && \
  pip install --no-cache -r requirements/ingest-elasticsearch.txt && \
  pip install --no-cache -r requirements/ingest-gcs.txt && \
  pip install --no-cache -r requirements/ingest-github.txt && \
  pip install --no-cache -r requirements/ingest-gitlab.txt && \
  pip install --no-cache -r requirements/ingest-google-drive.txt && \
  pip install --no-cache -r requirements/ingest-notion.txt && \
  pip install --no-cache -r requirements/ingest-onedrive.txt && \
  pip install --no-cache -r requirements/ingest-outlook.txt && \
  pip install --no-cache -r requirements/ingest-reddit.txt && \
  pip install --no-cache -r requirements/ingest-s3.txt && \
  pip install --no-cache -r requirements/ingest-slack.txt && \
  pip install --no-cache -r requirements/ingest-wikipedia.txt && \
  pip install --no-cache -r requirements/extra-csv.txt && \
  pip install --no-cache -r requirements/extra-docx.txt && \
  pip install --no-cache -r requirements/extra-epub.txt && \
  pip install --no-cache -r requirements/extra-markdown.txt && \
  pip install --no-cache -r requirements/extra-msg.txt && \
  pip install --no-cache -r requirements/extra-odt.txt && \
  pip install --no-cache -r requirements/extra-pandoc.txt && \
  pip install --no-cache -r requirements/extra-pdf-image.txt && \
  pip install --no-cache -r requirements/extra-pptx.txt && \
  pip install --no-cache -r requirements/extra-xlsx.txt && \
  dnf -y groupremove "Development Tools" && \
  dnf clean all

RUN python3.10 -c "import nltk; nltk.download('punkt')" && \
  python3.10 -c "import nltk; nltk.download('averaged_perceptron_tagger')"

FROM deps as code

COPY example-docs example-docs
COPY unstructured unstructured

RUN python3.10 -c "from unstructured.ingest.doc_processor.generalized import initialize; initialize()"

CMD ["/bin/bash"]
