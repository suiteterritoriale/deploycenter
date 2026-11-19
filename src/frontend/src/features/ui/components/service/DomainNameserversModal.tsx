import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon, Spinner } from "@gouvfr-lasuite/ui-components";
import { Button, Input, Modal, ModalSize } from "@gouvfr-lasuite/ui-components";
import { errorToString } from "@/features/api/APIError";
import { DomainRegistration } from "@/features/api/Repository";
import { useOperatorContext } from "@/features/layouts/components/GlobalLayout";
import { useMutationUpdateDomainNameservers } from "@/hooks/useQueries";

const PREFIX = "organizations.services.types.domain_registration.nameservers_modal";

// What the backend accepts: at least two nameservers, at most five.
const MIN_NAMESERVERS = 2;
const MAX_NAMESERVERS = 5;

export type DomainNameserversModalProps = {
  isOpen: boolean;
  onClose: () => void;
  organizationId: string;
  registration: DomainRegistration;
};

/**
 * Points a registered domain at other nameservers. Independent of the
 * registration: it works on any domain of the list once it exists. The change is
 * asked of the registrar by the backend, which reports what the registrar says.
 */
export const DomainNameserversModal = (props: DomainNameserversModalProps) => {
  const { t } = useTranslation();
  const { operatorId } = useOperatorContext();
  const update = useMutationUpdateDomainNameservers(
    operatorId,
    props.organizationId
  );
  const [nameservers, setNameservers] = useState<string[]>(() => {
    const current = props.registration.desired_nameservers;
    return current.length >= MIN_NAMESERVERS
      ? current
      : [...current, ...Array(MIN_NAMESERVERS - current.length).fill("")];
  });
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const filled = nameservers.map((name) => name.trim()).filter(Boolean);
  const canSubmit =
    filled.length >= MIN_NAMESERVERS && !update.isPending;

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSubmit) {
      return;
    }
    setErrorMessage(null);
    update.mutate(
      { id: props.registration.id, nameservers: filled },
      {
        onSuccess: () => props.onClose(),
        onError: (error) => setErrorMessage(errorToString(error)),
      }
    );
  };

  return (
    <Modal
      size={ModalSize.MEDIUM}
      title={`${t(`${PREFIX}.title`)} ${props.registration.domain}`}
      closeOnEsc={!update.isPending}
      closeOnClickOutside={!update.isPending}
      isOpen={props.isOpen}
      onClose={update.isPending ? () => {} : props.onClose}
      rightActions={
        <>
          <Button
            type="button"
            onClick={props.onClose}
            variant="secondary"
            disabled={update.isPending}
          >
            {t("common.cancel")}
          </Button>
          <Button
            type="submit"
            form="domain-nameservers-form"
            disabled={!canSubmit}
            icon={update.isPending ? <Spinner /> : undefined}
          >
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="dc__service__attribute__modal__content">
        <p className="dc__service__attribute__modal__content__help">
          {t(`${PREFIX}.description`)}
        </p>
        <form
          id="domain-nameservers-form"
          className="dc__domain-registration__form"
          onSubmit={handleSubmit}
        >
          {nameservers.map((nameserver, index) => (
            <div key={index} className="dc__domain-registration__nameserver">
              <Input
                label={t(`${PREFIX}.nameserver`, { index: index + 1 })}
                value={nameserver}
                onChange={(e) =>
                  setNameservers((previous) =>
                    previous.map((name, i) =>
                      i === index ? e.target.value : name
                    )
                  )
                }
              />
              {nameservers.length > MIN_NAMESERVERS && (
                <Button
                  type="button"
                  size="small"
                  variant="secondary"
                  icon={<Icon name="delete" />}
                  title={t(`${PREFIX}.delete`)}
                  onClick={() =>
                    setNameservers((previous) =>
                      previous.filter((_, i) => i !== index)
                    )
                  }
                />
              )}
            </div>
          ))}
          {nameservers.length < MAX_NAMESERVERS && (
            <Button
              type="button"
              variant="secondary"
              onClick={() => setNameservers((previous) => [...previous, ""])}
            >
              {t(`${PREFIX}.add`)}
            </Button>
          )}
          {errorMessage && (
            <p className="dc__domain-selector__error">{errorMessage}</p>
          )}
        </form>
      </div>
    </Modal>
  );
};
